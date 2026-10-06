"""Run one closed seven-day staging outcome evaluation window."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence import persistence
from src.analysis.open_intelligence.outcome_job import (
    EVALUATION_VERSION_V2,
    run_outcome_evaluation,
)
from src.analysis.open_intelligence.outcome_reader import read_written_outcomes
from src.analysis.open_intelligence.outcome_reporting import (
    attributed_cohort_rows,
    closed_cohort_record,
    cohort_metrics,
    verify_closed_cohort_record,
)
from src.analysis.open_intelligence.outcome_review_reader import (
    OutcomeReviewRefusal,
    load_outcome_review_records,
)
from src.analysis.open_intelligence.outcomes import OutcomeRules
from src.analysis.open_intelligence.source_yield import CreditEnvelope, SourceYieldRules
from src.contracts.open_intelligence import OUTCOME_STATES

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
WRITER_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
RULE_VERSION = "outcome_rules_v1"
RUN_ID_PREFIX = "outcome_eval_v1"
# A review records document runs the week under the v2 evaluation version, whose
# run ids are a new immutable natural key: nothing written under v1 is rewritten.
# Without one the caller refuses (review_records_required); the v1 path with its
# empty review maps runs only behind the explicit legacy flag.
RUN_ID_PREFIX_V2 = "outcome_eval_v2"
RULE_CONFIG_PATH = ROOT / "configs" / "open_intelligence_outcome_rules.yaml"
# The measured allocation reads every source number from one immutable document.
# There is no default rule set, envelope or measurement: a number nobody recorded
# has no substitute, so the week simply carries no allocation without the file.
SOURCE_MEASUREMENTS_CONTRACT = "outcome_source_measurements_v1"
# The numbers and the rules that judge them travel under the same proof the
# rule config beside them carries: a replay certified status, a pinned rule
# version, a replay receipt, a named approver and an approval window checked
# against the clock. A document that only names its contract proves nothing.
SOURCE_MEASUREMENTS_RULE_VERSION = "outcome_source_measurement_rules_v1"
SOURCE_MEASUREMENT_DOCUMENT_FIELDS = (
    "contract_version",
    "status",
    "rule_version",
    "replay_receipt_id",
    "approved_by",
    "approved_at",
    "expires_at",
    "minimum_due",
    "source_yield_rules",
    "credit_envelope",
    "route_caps",
    "measurements",
)


# One authorised write is bound to the dry run that preceded it. The dry run
# publishes a receipt over everything the write will be judged by; a grant names
# that receipt by digest inside a bounded window, and is consumed exactly once.
DRY_RUN_RECEIPT_CONTRACT = "outcome_dry_run_receipt_v1"
DRY_RUN_RECEIPT_DIGEST_FIELD = "receipt_digest"
DRY_RUN_RECEIPT_FIELDS = (
    "contract_version",
    "week_ending",
    "client_scope_id",
    "evaluation_version",
    "evaluation_run_ids",
    "dates",
    "prediction_ids",
    "review_records_digest",
    "source_measurements_digest",
)
WRITE_GRANT_FIELDS = (
    "grant_id",
    "dry_run_receipt_sha256",
    "granted_at",
    "expires_at",
    "grantor",
)
WRITE_GRANT_MAXIMUM_WINDOW = timedelta(hours=8)
# Under the v2 evaluation version every review input comes from the review
# records document and the job refuses any external map, so the caller hands it
# one named, read only, empty mapping rather than a literal it could fill.
NO_EXTERNAL_REVIEW_INPUTS: Mapping[str, object] = MappingProxyType({})


class OutcomeCliRefusal(ValueError):
    """The weekly request is outside the approved staging contract."""


@dataclass(frozen=True, slots=True)
class CertifiedRuleBundle:
    status: str
    rule_version: str
    replay_receipt_id: str
    approved_at: datetime
    approved_by: str
    expires_at: datetime


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal(f"rule {field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OutcomeCliRefusal(f"rule {field} is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeCliRefusal(f"rule {field} must be timezone aware")
    return parsed.astimezone(UTC)


def _required_text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal(f"rule {field} is missing")
    return value.strip()


def load_certified_rules(path: Path, now: datetime) -> CertifiedRuleBundle:
    if not path.is_file():
        raise OutcomeCliRefusal("rule config is missing")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise OutcomeCliRefusal("rule config is malformed") from error
    if not isinstance(document, Mapping):
        raise OutcomeCliRefusal("rule config must be a mapping")
    if document.get("status") != "replay_certified":
        raise OutcomeCliRefusal("rule config is not replay certified")
    rule_version = _required_text(document, "rule_version")
    if rule_version != RULE_VERSION:
        raise OutcomeCliRefusal("rule version must be outcome_rules_v1")
    approved_at = _timestamp(document.get("approved_at"), "approved_at")
    expires_at = _timestamp(document.get("expires_at"), "expires_at")
    if approved_at > now:
        raise OutcomeCliRefusal("rule approval is in the future")
    if expires_at <= now:
        raise OutcomeCliRefusal("rule certification is expired")
    if expires_at <= approved_at:
        raise OutcomeCliRefusal("rule certification window is malformed")
    return CertifiedRuleBundle(
        status="replay_certified",
        rule_version=rule_version,
        replay_receipt_id=_required_text(document, "replay_receipt_id"),
        approved_at=approved_at,
        approved_by=_required_text(document, "approved_by"),
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class SourceMeasurementBundle:
    """One immutable measurement document: rules, envelope, caps and per source values."""

    minimum_due: int
    rules: SourceYieldRules
    envelope: CreditEnvelope
    route_caps: Mapping[str, int]
    measurements: Mapping[str, Mapping[str, object]]
    rule_version: str
    document_digest: str


def _measurement_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal(f"source_measurements_{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OutcomeCliRefusal(f"source_measurements_{field}_malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeCliRefusal(f"source_measurements_{field}_malformed")
    return parsed.astimezone(UTC)


def _measurement_text(document: Mapping[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal(f"source_measurements_{field}_missing")
    return value.strip()


def _read_measurement_document(path: Path) -> tuple[Mapping[str, object], str]:
    """Read the document once and digest the very bytes that were admitted.

    The digest is taken over the bytes, not over the parsed object, so the same
    values written two ways are two documents and a document cannot be
    substituted between an admission and the digest that names it.
    """
    if not isinstance(path, Path) or not path.is_file():
        raise OutcomeCliRefusal("source_measurements_unreadable")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise OutcomeCliRefusal("source_measurements_unreadable") from error
    if not isinstance(document, Mapping):
        raise OutcomeCliRefusal("source_measurements_contract_invalid")
    if document.get("contract_version") != SOURCE_MEASUREMENTS_CONTRACT:
        raise OutcomeCliRefusal("source_measurements_contract_invalid")
    if tuple(document) != SOURCE_MEASUREMENT_DOCUMENT_FIELDS:
        raise OutcomeCliRefusal("source_measurements_fields_invalid")
    return document, hashlib.sha256(raw).hexdigest()


def source_measurements_digest(path: Path) -> str:
    """Digest the measurement document exactly as it was read."""
    return _read_measurement_document(path)[1]


def load_source_measurements(path: Path, now: datetime) -> SourceMeasurementBundle:
    """Admit the measurement document. An empty measurement set is not a measurement."""
    document, document_digest = _read_measurement_document(path)
    if document.get("status") != "replay_certified":
        raise OutcomeCliRefusal("source_measurements_not_certified")
    if _measurement_text(document, "rule_version") != SOURCE_MEASUREMENTS_RULE_VERSION:
        raise OutcomeCliRefusal("source_measurements_rule_version_invalid")
    _measurement_text(document, "replay_receipt_id")
    _measurement_text(document, "approved_by")
    approved_at = _measurement_timestamp(document.get("approved_at"), "approved_at")
    expires_at = _measurement_timestamp(document.get("expires_at"), "expires_at")
    if approved_at > now:
        raise OutcomeCliRefusal("source_measurements_approval_in_the_future")
    if expires_at <= now:
        raise OutcomeCliRefusal("source_measurements_certification_expired")
    if expires_at <= approved_at:
        raise OutcomeCliRefusal("source_measurements_certification_window_malformed")
    measurements = document["measurements"]
    if not isinstance(measurements, Mapping) or not measurements:
        raise OutcomeCliRefusal("source_measurements_empty")
    if any(not isinstance(value, Mapping) for value in measurements.values()):
        raise OutcomeCliRefusal("source_measurements_fields_invalid")
    minimum_due = document["minimum_due"]
    if type(minimum_due) is not int or minimum_due < 1:
        raise OutcomeCliRefusal("source_measurements_fields_invalid")
    route_caps = document["route_caps"]
    if not isinstance(route_caps, Mapping) or not route_caps:
        raise OutcomeCliRefusal("source_measurements_fields_invalid")
    for section in ("source_yield_rules", "credit_envelope"):
        if not isinstance(document[section], Mapping):
            raise OutcomeCliRefusal("source_measurements_fields_invalid")
    try:
        rules = SourceYieldRules(**dict(document["source_yield_rules"]))
        envelope = CreditEnvelope(**dict(document["credit_envelope"]))
    except (TypeError, ValueError) as error:
        raise OutcomeCliRefusal("source_measurements_fields_invalid") from error
    return SourceMeasurementBundle(
        minimum_due=minimum_due,
        rules=rules,
        envelope=envelope,
        route_caps=dict(route_caps),
        measurements={family: dict(value) for family, value in measurements.items()},
        rule_version=SOURCE_MEASUREMENTS_RULE_VERSION,
        document_digest=document_digest,
    )


def _carried_outcome_rows(
    result: object,
    *,
    evaluation_run_id: str,
    evaluation_date: date,
    client_scope_id: str,
) -> tuple[Mapping[str, object], ...]:
    """Take the rows the run actually wrote, and only those.

    Absent rows are refused, never scored as none. Every row is checked against
    the run, the day and the client scope this caller asked for, so a row
    carrying another run's identifier is refused instead of published as this
    week's provenance. Identifiers are stripped before they are judged nonempty.
    """
    rows = getattr(result, "outcome_rows", None)
    written = getattr(result, "outcome_row_count", None)
    if (
        rows is None
        or not isinstance(rows, (tuple, list))
        or any(not isinstance(row, Mapping) for row in rows)
        or type(written) is not int
        or len(rows) != written
    ):
        raise OutcomeCliRefusal("outcome_rows_missing")
    for row in rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise OutcomeCliRefusal("evaluation_run_id_required")
        if run_id.strip() != evaluation_run_id:
            raise OutcomeCliRefusal("evaluation_run_id_unexpected")
        scope = row.get("client_scope_id")
        if not isinstance(scope, str) or not scope.strip():
            raise OutcomeCliRefusal("client_scope_required")
        if scope.strip() != client_scope_id:
            raise OutcomeCliRefusal("client_scope_mismatch")
        day = row.get("evaluation_date")
        if isinstance(day, str):
            try:
                day = date.fromisoformat(day)
            except ValueError as error:
                raise OutcomeCliRefusal("outcome_row_date_unexpected") from error
        if isinstance(day, datetime) or not isinstance(day, date) or day != evaluation_date:
            raise OutcomeCliRefusal("outcome_row_date_unexpected")
    return tuple(rows)


def _week_dates(week_ending: date) -> tuple[date, ...]:
    if isinstance(week_ending, datetime) or not isinstance(week_ending, date):
        raise OutcomeCliRefusal("week ending must be a date")
    if week_ending.weekday() != 6:
        raise OutcomeCliRefusal("week ending must be a Sunday")
    monday = week_ending - timedelta(days=6)
    return tuple(monday + timedelta(days=offset) for offset in range(7))


def _client_scope(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal("client scope ID must be a nonempty string")
    return value.strip()


def _default_client_factory(*, project: str, location: str) -> object:
    from google.cloud import bigquery

    return bigquery.Client(project=project, location=location)


def _validate_client(client: object, *, apply: bool) -> None:
    if getattr(client, "project", None) != PROJECT or getattr(client, "location", None) != LOCATION:
        raise OutcomeCliRefusal("client must use the exact staging target")
    if not apply:
        return
    try:
        persistence.validate_real_client(client, PROJECT)
    except persistence.TargetInvalid as error:
        raise OutcomeCliRefusal("client must use the exact staging apply identity") from error


def _count(mapping: object, field: str) -> int:
    if not isinstance(mapping, Mapping):
        raise OutcomeCliRefusal("persistence receipt counts are invalid")
    value = mapping.get("outcomes")
    if type(value) is not int or value < 0:
        raise OutcomeCliRefusal(f"persistence {field} count is invalid")
    return value


def _persistence_payload(result: object, *, dry_run: bool, outcome_rows: int) -> object:
    receipt = getattr(result, "persistence_result", None)
    if receipt is None:
        if outcome_rows != 0:
            raise OutcomeCliRefusal("nonempty date has no persistence receipt")
        return None
    if getattr(receipt, "dry_run", None) is not dry_run:
        raise OutcomeCliRefusal("persistence receipt mode is invalid")
    validated = _count(getattr(receipt, "validated_counts", None), "validated")
    inserted = _count(getattr(receipt, "inserted_counts", None), "inserted")
    unchanged = _count(getattr(receipt, "unchanged_counts", None), "unchanged")
    conflicts = _count(getattr(receipt, "conflict_counts", None), "conflict")
    cleanup_state = getattr(receipt, "cleanup_state", None)
    expected_cleanup = "not_started" if dry_run else "complete"
    if (
        validated != outcome_rows
        or conflicts != 0
        or cleanup_state != expected_cleanup
        or (dry_run and (inserted != 0 or unchanged != 0))
        or (not dry_run and inserted + unchanged != outcome_rows)
    ):
        raise OutcomeCliRefusal("persistence receipt is invalid")
    return {
        "validated_rows": validated,
        "inserted_rows": inserted,
        "unchanged_rows": unchanged,
        "conflict_rows": conflicts,
        "cleanup_state": cleanup_state,
    }


def _unreviewed_count(result: object, due: int) -> int:
    if getattr(result, "evaluation_version", None) != EVALUATION_VERSION_V2:
        raise OutcomeCliRefusal("outcome result evaluation version is invalid")
    unreviewed = getattr(result, "unreviewed_prediction_ids", None)
    if (
        not isinstance(unreviewed, (tuple, list))
        or len(unreviewed) > due
        or len(set(unreviewed)) != len(unreviewed)
    ):
        raise OutcomeCliRefusal("outcome result unreviewed predictions are invalid")
    return len(unreviewed)


def _date_payload(
    result: object,
    evaluation_date: date,
    dry_run: bool,
    *,
    run_id_prefix: str = RUN_ID_PREFIX,
    reviewed: bool = False,
) -> dict[str, object]:
    run_id = f"{run_id_prefix}_{evaluation_date.isoformat()}"
    if getattr(result, "evaluation_run_id", None) != run_id:
        raise OutcomeCliRefusal("outcome result run ID is invalid")
    due = getattr(result, "due_prediction_count", None)
    rows = getattr(result, "outcome_row_count", None)
    missing_ids = getattr(result, "missing_prediction_ids", None)
    state_counts = getattr(result, "state_counts", None)
    if (
        type(due) is not int
        or due < 0
        or type(rows) is not int
        or rows != due
        or not isinstance(missing_ids, (tuple, list))
        or len(missing_ids) > due
        or not isinstance(state_counts, Mapping)
        or tuple(state_counts) != OUTCOME_STATES
        or any(type(value) is not int or value < 0 for value in state_counts.values())
        or sum(state_counts.values()) != rows
    ):
        raise OutcomeCliRefusal("outcome result counts are invalid")
    payload = {
        "evaluation_date": evaluation_date.isoformat(),
        "evaluation_run_id": run_id,
        "due_predictions": due,
        "outcome_rows": rows,
        "missing_predictions": len(missing_ids),
        "state_counts": {state: state_counts[state] for state in OUTCOME_STATES},
        "persistence": _persistence_payload(result, dry_run=dry_run, outcome_rows=rows),
    }
    if reviewed:
        payload["unreviewed_predictions"] = _unreviewed_count(result, due)
    return payload


def _canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def dry_run_receipt(
    *,
    week_ending: date,
    client_scope_id: str,
    rows_by_run: tuple[tuple[date, str, tuple[Mapping[str, object], ...]], ...],
    review_records_digest: str,
    source_measurements_digest: str,
) -> dict[str, object]:
    """Receipt over everything a later write is bound to, digested canonically.

    It names the week, the client scope, the runs, the due, resolved and
    unresolved counts of each date with the outcome state counts behind them,
    every due prediction and the two input documents. Nothing here reads a clock, so the same live reads give the same
    receipt and a write can be refused when they no longer do.
    """
    dates = []
    prediction_ids: list[str] = []
    for evaluation_date, run_id, rows in rows_by_run:
        state_counts = dict.fromkeys(OUTCOME_STATES, 0)
        for row in rows:
            prediction_id = row.get("prediction_id")
            if not isinstance(prediction_id, str) or not prediction_id.strip():
                raise OutcomeCliRefusal("prediction_id_required")
            prediction_ids.append(prediction_id.strip())
            outcome = row.get("outcome")
            if outcome not in state_counts:
                raise OutcomeCliRefusal("outcome_state_invalid")
            state_counts[outcome] += 1
        unresolved = state_counts["unresolved"]
        dates.append(
            {
                "evaluation_date": evaluation_date.isoformat(),
                "evaluation_run_id": run_id,
                "due": len(rows),
                "resolved": len(rows) - unresolved,
                "unresolved": unresolved,
                "state_counts": state_counts,
            }
        )
    body: dict[str, object] = {
        "contract_version": DRY_RUN_RECEIPT_CONTRACT,
        "week_ending": week_ending.isoformat(),
        "client_scope_id": client_scope_id,
        "evaluation_version": EVALUATION_VERSION_V2,
        "evaluation_run_ids": [run_id for _, run_id, _ in rows_by_run],
        "dates": dates,
        "prediction_ids": sorted(prediction_ids),
        "review_records_digest": review_records_digest,
        "source_measurements_digest": source_measurements_digest,
    }
    if tuple(body) != DRY_RUN_RECEIPT_FIELDS:
        raise OutcomeCliRefusal("dry_run_receipt_field_drift")
    body[DRY_RUN_RECEIPT_DIGEST_FIELD] = _canonical_digest(dict(body))
    return body


def _load_dry_run_receipt(path: Path) -> str:
    """Admit a saved receipt only when its own body still carries its digest."""
    try:
        document = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise OutcomeCliRefusal("dry_run_receipt_invalid") from error
    if (
        not isinstance(document, Mapping)
        or set(document) != {*DRY_RUN_RECEIPT_FIELDS, DRY_RUN_RECEIPT_DIGEST_FIELD}
        or document.get("contract_version") != DRY_RUN_RECEIPT_CONTRACT
        or not _is_sha256(document.get(DRY_RUN_RECEIPT_DIGEST_FIELD))
    ):
        raise OutcomeCliRefusal("dry_run_receipt_invalid")
    body = {key: value for key, value in document.items() if key != DRY_RUN_RECEIPT_DIGEST_FIELD}
    digest = document[DRY_RUN_RECEIPT_DIGEST_FIELD]
    if _canonical_digest(body) != digest:
        raise OutcomeCliRefusal("dry_run_receipt_mismatch")
    return digest


@dataclass(frozen=True, slots=True)
class WriteGrant:
    grant_id: str
    dry_run_receipt_sha256: str
    granted_at: datetime
    expires_at: datetime
    grantor: str


def _grant_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeCliRefusal("write_grant_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OutcomeCliRefusal("write_grant_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutcomeCliRefusal("write_grant_invalid")
    return parsed.astimezone(UTC)


# A grant id names its consumed marker file, so it holds only file safe characters.
_GRANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def consumed_marker_name(grant: WriteGrant) -> str:
    """The one file name a grant may be consumed under, fixed by the grant itself.

    The name carries the grant id and the start of the receipt digest it names,
    so a second consumed path for the same grant cannot be chosen freely: any
    other name is refused, and the derived name already exists once used.
    """
    return f"consumed-{grant.grant_id}-{grant.dry_run_receipt_sha256[:16]}.json"


def load_write_grant(path: Path, now: datetime) -> WriteGrant:
    """Admit one write grant: exact fields, a bounded window, and valid now."""
    try:
        document = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise OutcomeCliRefusal("write_grant_invalid") from error
    if not isinstance(document, Mapping) or set(document) != set(WRITE_GRANT_FIELDS):
        raise OutcomeCliRefusal("write_grant_invalid")
    texts = {}
    for field in ("grant_id", "grantor"):
        value = document[field]
        if not isinstance(value, str) or not value.strip():
            raise OutcomeCliRefusal("write_grant_invalid")
        texts[field] = value.strip()
    if _GRANT_ID.fullmatch(texts["grant_id"]) is None:
        raise OutcomeCliRefusal("write_grant_invalid")
    if not _is_sha256(document["dry_run_receipt_sha256"]):
        raise OutcomeCliRefusal("write_grant_invalid")
    granted_at = _grant_timestamp(document["granted_at"])
    expires_at = _grant_timestamp(document["expires_at"])
    if expires_at <= granted_at or expires_at - granted_at > WRITE_GRANT_MAXIMUM_WINDOW:
        raise OutcomeCliRefusal("write_grant_invalid")
    if now < granted_at or now >= expires_at:
        raise OutcomeCliRefusal("write_grant_expired")
    return WriteGrant(
        grant_id=texts["grant_id"],
        dry_run_receipt_sha256=document["dry_run_receipt_sha256"],
        granted_at=granted_at,
        expires_at=expires_at,
        grantor=texts["grantor"],
    )


def _consume_grant(
    path: Path, grant: WriteGrant, *, now: datetime, week_ending: date, client_scope_id: str
) -> None:
    """Create the consumed marker exclusively. A marker already there is a used grant."""
    marker = {
        "grant_id": grant.grant_id,
        "dry_run_receipt_sha256": grant.dry_run_receipt_sha256,
        "consumed_at": now.isoformat(),
        "week_ending": week_ending.isoformat(),
        "client_scope_id": client_scope_id,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(marker, sort_keys=True, separators=(",", ":")))
    except FileExistsError as error:
        raise OutcomeCliRefusal("write_grant_consumed") from error
    except OSError as error:
        raise OutcomeCliRefusal("consumed_grant_path_unwritable") from error


def _keep_allocation(path: Path, record: Mapping[str, object]) -> None:
    """Write the allocation record once, then prove the kept copy still verifies."""
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
    except FileExistsError as error:
        raise OutcomeCliRefusal("allocation_out_exists") from error
    except OSError as error:
        raise OutcomeCliRefusal("allocation_out_unwritable") from error
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        verify_closed_cohort_record(kept)
    except (OSError, UnicodeError, ValueError) as error:
        raise OutcomeCliRefusal(f"allocation_out_unverified: {error}") from error
    if kept != record:
        raise OutcomeCliRefusal("allocation_out_unverified")


def _row_values(rows: object) -> list[dict[str, object]]:
    return [dict(row) for row in rows]


def _rows_by_run(
    rows: tuple[Mapping[str, object], ...], dated_runs: tuple[tuple[date, str], ...]
) -> tuple[tuple[date, str, tuple[Mapping[str, object], ...]], ...]:
    return tuple(
        (evaluation_date, run_id, tuple(row for row in rows if row.get("run_id") == run_id))
        for evaluation_date, run_id in dated_runs
    )


def run_weekly_outcomes(
    *,
    week_ending: date,
    client_scope_id: str,
    apply: bool,
    client_factory: Callable[..., object] = _default_client_factory,
    job_runner: Callable[..., object] = run_outcome_evaluation,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    rule_config_path: Path = RULE_CONFIG_PATH,
    review_records_path: Path | None = None,
    legacy_v1: bool = False,
    measurements_path: Path | None = None,
    dry_run_receipt_path: Path | None = None,
    write_grant_path: Path | None = None,
    consumed_grant_path: Path | None = None,
    allocation_out_path: Path | None = None,
    outcome_readback: Callable[..., object] = read_written_outcomes,
) -> dict[str, object]:
    if type(apply) is not bool:
        raise OutcomeCliRefusal("apply must be boolean")
    if type(legacy_v1) is not bool:
        raise OutcomeCliRefusal("legacy v1 must be boolean")
    dates = _week_dates(week_ending)
    client_scope_id = _client_scope(client_scope_id)
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise OutcomeCliRefusal("clock must return a timezone-aware timestamp")
    now = now.astimezone(UTC)
    if week_ending >= now.date():
        raise OutcomeCliRefusal("week ending must be a closed Sunday")
    rule_bundle = load_certified_rules(rule_config_path, now)
    if review_records_path is None and not legacy_v1:
        raise OutcomeCliRefusal("review_records_required")
    if legacy_v1 and review_records_path is not None:
        raise OutcomeCliRefusal("legacy_v1_conflicts_with_review_records")
    if legacy_v1 and measurements_path is not None:
        raise OutcomeCliRefusal("legacy_v1_conflicts_with_measurements")
    authority_paths = (
        dry_run_receipt_path,
        write_grant_path,
        consumed_grant_path,
        allocation_out_path,
    )
    bound = any(item is not None for item in authority_paths)
    if legacy_v1 and bound:
        raise OutcomeCliRefusal("legacy_v1_conflicts_with_write_grant")
    if bound and not apply:
        raise OutcomeCliRefusal("write_authority_requires_apply")
    # A reviewed write is the closed cohort write: it needs the measurements the
    # cohort is scored by and every piece of authority, or nothing is built.
    if apply and not legacy_v1:
        if measurements_path is None:
            raise OutcomeCliRefusal("closed_cohort_measurements_required")
        for value, name in zip(
            authority_paths,
            (
                "dry_run_receipt_required",
                "write_grant_required",
                "consumed_grant_path_required",
                "allocation_out_required",
            ),
            strict=True,
        ):
            if value is None:
                raise OutcomeCliRefusal(name)
    measurements = (
        None if measurements_path is None else load_source_measurements(measurements_path, now)
    )
    review_records = None
    if review_records_path is not None:
        try:
            review_records = load_outcome_review_records(review_records_path)
        except OutcomeReviewRefusal as error:
            raise OutcomeCliRefusal(f"review records are invalid: {error}") from error
    reviewed = review_records is not None
    run_id_prefix = RUN_ID_PREFIX_V2 if reviewed else RUN_ID_PREFIX
    dry_run = not apply
    outcome_rules = OutcomeRules(rule_version=rule_bundle.rule_version)
    dated_runs = tuple(
        (evaluation_date, f"{run_id_prefix}_{evaluation_date.isoformat()}")
        for evaluation_date in dates
    )
    evaluation_run_ids = [run_id for _, run_id in dated_runs]

    def evaluate_week(
        client: object,
        week_dry_run: bool,
        verified: Mapping[str, object] | None = None,
        checked: dict[str, object] | None = None,
    ) -> tuple[list[dict[str, object]], tuple[Mapping[str, object], ...]]:
        """Evaluate each day, or on a bound write persist each day's checked rows.

        With verified results the runner is handed the exact dry run result the
        receipt check accepted and persists its rows without evaluating again,
        and each written day must carry back exactly those rows.
        """
        date_results = []
        cohort_rows: list[Mapping[str, object]] = []
        for evaluation_date, run_id in dated_runs:
            review_options: dict[str, object] = {}
            if reviewed:
                review_options = {
                    "evaluation_version": EVALUATION_VERSION_V2,
                    "review_records": review_records,
                    "quality_findings_by_prediction": NO_EXTERNAL_REVIEW_INPUTS,
                    "human_calibrations_by_prediction": NO_EXTERNAL_REVIEW_INPUTS,
                }
            else:
                # Legacy v1 only: no review input exists, which LEGACY_V1_NOTICE says.
                review_options = {
                    "quality_findings_by_prediction": {},
                    "human_calibrations_by_prediction": {},
                }
            # The rows travel back only when this week asked for a measured
            # allocation; a week that asked for none carries none.
            if measurements is not None:
                review_options["carry_outcome_rows"] = True
            if verified is not None:
                review_options["verified_result"] = verified[run_id]
            result = job_runner(
                client=client,
                dataset=DATASET,
                evaluation_date=evaluation_date,
                evaluated_at=now,
                client_scope_id=client_scope_id,
                evaluation_run_id=run_id,
                outcome_rules=outcome_rules,
                persistence_rule_bundle=rule_bundle,
                dry_run=week_dry_run,
                **review_options,
            )
            if checked is not None:
                checked[run_id] = result
            if measurements is not None:
                day_rows = _carried_outcome_rows(
                    result,
                    evaluation_run_id=run_id,
                    evaluation_date=evaluation_date,
                    client_scope_id=client_scope_id,
                )
                if verified is not None and _row_values(day_rows) != _row_values(
                    verified[run_id].outcome_rows
                ):
                    raise OutcomeCliRefusal("write_rows_unverified")
                cohort_rows.extend(day_rows)
            date_results.append(
                _date_payload(
                    result,
                    evaluation_date,
                    week_dry_run,
                    run_id_prefix=run_id_prefix,
                    reviewed=reviewed,
                )
            )
        return date_results, tuple(cohort_rows)

    def receipt_for(rows: tuple[Mapping[str, object], ...]) -> dict[str, object]:
        return dry_run_receipt(
            week_ending=week_ending,
            client_scope_id=client_scope_id,
            rows_by_run=_rows_by_run(rows, dated_runs),
            review_records_digest=review_records.document_digest,
            source_measurements_digest=measurements.document_digest,
        )

    grant = None
    verified_results: dict[str, object] | None = None
    if bound:
        grant = load_write_grant(write_grant_path, now)
        receipt_digest = _load_dry_run_receipt(dry_run_receipt_path)
        if grant.dry_run_receipt_sha256 != receipt_digest:
            raise OutcomeCliRefusal("dry_run_receipt_mismatch")
        if Path(consumed_grant_path).name != consumed_marker_name(grant):
            raise OutcomeCliRefusal("write_grant_consumed_path_unbound")
        if consumed_grant_path.exists():
            raise OutcomeCliRefusal("write_grant_consumed")
        if allocation_out_path.exists():
            raise OutcomeCliRefusal("allocation_out_exists")
        # The dry run is recomputed from live reads before any client is built
        # for writing; a receipt the live reads no longer reproduce is refused.
        check_client = client_factory(project=PROJECT, location=LOCATION)
        _validate_client(check_client, apply=False)
        verified_results = {}
        _, live_rows = evaluate_week(check_client, True, checked=verified_results)
        if receipt_for(live_rows)[DRY_RUN_RECEIPT_DIGEST_FIELD] != receipt_digest:
            raise OutcomeCliRefusal("dry_run_receipt_mismatch")
        # A marker is local; the store is not. Rows already written for these
        # evaluation runs mean this cohort was written, whatever marker path is given.
        already_written = tuple(
            outcome_readback(
                client=check_client,
                dataset=DATASET,
                client_scope_id=client_scope_id,
                evaluation_run_ids=tuple(evaluation_run_ids),
                evaluation_date_from=dates[0],
                evaluation_date_to=dates[-1],
            )
        )
        if already_written:
            raise OutcomeCliRefusal("write_grant_consumed_in_store")
        _consume_grant(
            consumed_grant_path,
            grant,
            now=now,
            week_ending=week_ending,
            client_scope_id=client_scope_id,
        )
    client = client_factory(project=PROJECT, location=LOCATION)
    _validate_client(client, apply=apply)
    date_results, cohort_rows = evaluate_week(client, dry_run, verified=verified_results)
    payload: dict[str, object] = {
        "week_ending": week_ending.isoformat(),
        "client_scope_id": client_scope_id,
        "dry_run": dry_run,
    }
    if reviewed:
        payload["evaluation_version"] = EVALUATION_VERSION_V2
        payload["review_records_digest"] = review_records.document_digest
    payload["dates"] = date_results
    totals = {
        "due_predictions": sum(item["due_predictions"] for item in date_results),
        "outcome_rows": sum(item["outcome_rows"] for item in date_results),
        "missing_predictions": sum(item["missing_predictions"] for item in date_results),
    }
    if reviewed:
        totals["unreviewed_predictions"] = sum(
            item["unreviewed_predictions"] for item in date_results
        )
    payload["totals"] = totals
    if measurements is not None:
        payload["source_measurements_digest"] = measurements.document_digest

        def record_for(rows: tuple[Mapping[str, object], ...]) -> dict:
            record = closed_cohort_record(
                rows,
                cohort_close=week_ending,
                client_scope_id=client_scope_id,
                evaluation_version=EVALUATION_VERSION_V2,
                rule_version=measurements.rule_version,
                measurement_document_digest=measurements.document_digest,
                evaluation_run_ids=tuple(evaluation_run_ids),
                minimum_due=measurements.minimum_due,
                measurements=measurements.measurements,
                rules=measurements.rules,
                envelope=measurements.envelope,
                route_caps=measurements.route_caps,
            )
            verify_closed_cohort_record(record)
            return record

        try:
            record = record_for(cohort_rows)
        except ValueError as error:
            raise OutcomeCliRefusal(f"measured allocation is unavailable: {error}") from error
        payload["measured_allocation"] = record
        receipt = receipt_for(cohort_rows)
        if dry_run:
            payload["dry_run_receipt"] = receipt
        elif grant is not None:
            if receipt[DRY_RUN_RECEIPT_DIGEST_FIELD] != grant.dry_run_receipt_sha256:
                raise OutcomeCliRefusal("dry_run_receipt_mismatch")
            payload["write_authority"] = {
                "grant_id": grant.grant_id,
                "dry_run_receipt_sha256": grant.dry_run_receipt_sha256,
            }
            payload["readback"] = _read_back(
                client,
                outcome_readback=outcome_readback,
                client_scope_id=client_scope_id,
                evaluation_run_ids=tuple(evaluation_run_ids),
                evaluation_dates=(dates[0], dates[-1]),
                written_record=record,
                written_receipt=receipt,
                record_for=record_for,
                receipt_for=receipt_for,
            )
            _keep_allocation(allocation_out_path, record)
    return payload


def _read_back(
    client: object,
    *,
    outcome_readback: Callable[..., object],
    client_scope_id: str,
    evaluation_run_ids: tuple[str, ...],
    evaluation_dates: tuple[date, date],
    written_record: Mapping[str, object],
    written_receipt: Mapping[str, object],
    record_for: Callable[[tuple[Mapping[str, object], ...]], dict],
    receipt_for: Callable[[tuple[Mapping[str, object], ...]], dict],
) -> dict[str, object]:
    """Read the written rows back and rebuild the metrics and record from them.

    The cohort metrics, the closed cohort record and the receipt are recomputed
    from what the table now holds; any difference from what was written is a
    readback mismatch, and the allocation is not kept.
    """
    try:
        rows = tuple(
            outcome_readback(
                client=client,
                dataset=DATASET,
                client_scope_id=client_scope_id,
                evaluation_run_ids=evaluation_run_ids,
                evaluation_date_from=evaluation_dates[0],
                evaluation_date_to=evaluation_dates[1],
            )
        )
        readback_record = record_for(rows)
        readback_receipt = receipt_for(rows)
        readback_metrics = cohort_metrics(attributed_cohort_rows(rows))
    except (TypeError, ValueError) as error:
        raise OutcomeCliRefusal(f"readback_mismatch: {error}") from error
    if (
        readback_metrics != written_record["cohort"]
        or readback_record != written_record
        or readback_receipt != written_receipt
    ):
        raise OutcomeCliRefusal("readback_mismatch")
    return {
        "due": readback_metrics["due"],
        "resolved": readback_metrics["resolved"],
        "unresolved": readback_metrics["unresolved"],
        "allocation_proposal_digest": readback_record["record_digest"],
    }


def render_result(result: Mapping[str, object]) -> str:
    return json.dumps(result, separators=(",", ":"))


def _parse_week_ending(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error
    if parsed.weekday() != 6:
        raise argparse.ArgumentTypeError("week ending must be a Sunday")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--week-ending", required=True, type=_parse_week_ending)
    parser.add_argument("--client-scope-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--review-records", type=Path)
    parser.add_argument("--legacy-v1", action="store_true")
    parser.add_argument("--measurements", type=Path)
    parser.add_argument("--dry-run-receipt", type=Path)
    parser.add_argument("--write-grant", type=Path)
    parser.add_argument("--consumed-grant-path", type=Path)
    parser.add_argument("--allocation-out", type=Path)
    return parser


LEGACY_V1_NOTICE = (
    "notice: legacy v1 evaluation carries no review inputs; quality findings and "
    "human calibration maps are empty and no outcome it writes is reviewed"
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.legacy_v1:
        print(LEGACY_V1_NOTICE, file=sys.stderr)
    if args.apply and args.dry_run_receipt is None:
        print("error: dry_run_receipt_required", file=sys.stderr)
        return 1
    if args.apply and args.write_grant is None:
        print("error: write_grant_required", file=sys.stderr)
        return 1
    try:
        payload = run_weekly_outcomes(
            week_ending=args.week_ending,
            client_scope_id=args.client_scope_id,
            apply=args.apply,
            review_records_path=args.review_records,
            legacy_v1=args.legacy_v1,
            measurements_path=args.measurements,
            dry_run_receipt_path=args.dry_run_receipt,
            write_grant_path=args.write_grant,
            consumed_grant_path=args.consumed_grant_path,
            allocation_out_path=args.allocation_out,
        )
    except OutcomeCliRefusal as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("error: weekly outcome job failed", file=sys.stderr)
        return 1
    print(render_result(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
