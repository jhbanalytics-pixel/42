"""Compare successful capture records against the native jobs and artifacts they name."""

import json
import re
from datetime import UTC, datetime, timedelta

from .brain_contract import canonical_bytes, canonical_digest
from .daily_execution_authority import DailyAuthorityIntegrityError, require_daily_consumption
from .daily_execution_contracts import _digest, canonical_json_object
from .production_snapshot_storage import _CAPTURE_VERSION_V2, _validate_attempt, _validate_stored
from .source_estate_bridge import (
    PROFILE_FIELDS,
    PROJECT,
    RELATION_FIELDS,
    _check_clone_readbacks,
    _millis_instant,
    _observed,
    _parse_timestamp,
    validate_capture_facts,
)
from .source_estate_bridge_plan import validate_bridge_plan

_PROFILE_RESULT_FIELDS = PROFILE_FIELDS - {"schema_digest"}
_FIELDS = _PROFILE_RESULT_FIELDS | {
    "contract_version",
    "cutoff_date",
    "client_scope_id",
    "market_scope",
    "captured_at",
    "snapshot_plan_digest",
    "snapshot_digest",
    "capture_receipt_digest",
    "creation_records",
    "artifact_attempt",
    "stored_artifact",
    "query_count",
    "total_bytes_billed",
    "limitations",
    "missing_checks",
}
_CREATION_FIELDS = RELATION_FIELDS | {"job_id", "native_job_digest", "state"}


def _millis(value, code):
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(code) from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(code)
    whole, fraction = divmod(instant.microsecond, 1000)
    if fraction:
        raise ValueError(code)
    return str(int(instant.timestamp()) * 1000 + whole)


def _check_native_job(native, *, record, statement, completed_at, identity, code):
    """A creation record names one finished native CREATE SNAPSHOT TABLE job."""
    if type(native) is not dict:
        raise ValueError(code)
    project, dataset, table = record["destination_table"].split(".")
    status = native.get("status")
    statistics = native.get("statistics")
    configuration = native.get("configuration")
    if (
        type(status) is not dict
        or type(statistics) is not dict
        or type(configuration) is not dict
        or type(configuration.get("query")) is not dict
        or type(statistics.get("query")) is not dict
    ):
        raise ValueError(code)
    query = statistics["query"]
    if (
        native.get("jobReference")
        != {"projectId": PROJECT, "location": "US", "jobId": record["job_id"]}
        or native.get("user_email") != identity
        or configuration["query"].get("query") != statement["sql"]
        or status.get("state") != "DONE"
        or status.get("errorResult") is not None
        or query.get("statementType") != "CREATE_SNAPSHOT_TABLE"
        or query.get("ddlOperationPerformed") != "CREATE"
        or query.get("ddlTargetTable")
        != {"projectId": project, "datasetId": dataset, "tableId": table}
        or statistics.get("endTime") != _millis(completed_at, code)
        or record["native_job_digest"] != canonical_digest(native)
    ):
        raise ValueError(code)


def _check_job_window(native, *, earliest, latest, code):
    """A creation job created, started and ended inside [earliest, latest], compared in
    microseconds so a job may not begin in the millisecond before ``earliest``."""
    statistics = native["statistics"]
    times = []
    for field in ("creationTime", "startTime", "endTime"):
        value = statistics.get(field)
        if type(value) is not str or re.fullmatch(r"0|[1-9][0-9]{0,15}", value) is None:
            raise ValueError(code)
        times.append(int(value) * 1000)
    if not (_micros(earliest) <= times[0] <= times[1] <= times[2] <= _micros(latest)):
        raise ValueError(code)


def _micros(instant):
    return (instant - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def validate_successful_bridge_result(
    value,
    *,
    plan,
    plan_inputs,
    capture_facts,
    creation_records,
    creation_completed_at,
    artifact_attempt,
    stored_artifact,
    execution_status,
    native_metering,
    native_jobs,
    capture_consumption,
    clone_metadata,
):
    """``capture_consumption`` is the issued consumption the capture job runs under; its
    child service identity is the only principal whose jobs can create the clones.
    ``clone_metadata`` maps each destination table to its tables.get resource, read back
    after the jobs finished, and each recorded fingerprint is recomputed from it."""
    code = "source_bridge_result_invalid"

    def principal(profile):
        try:
            admitted = require_daily_consumption(
                capture_consumption, operation="daily_source_snapshot_capture"
            )
        except DailyAuthorityIntegrityError as error:
            raise ValueError(code) from error
        identity = admitted.operation_context["child_service_identity"]
        # The consumption must be the capture of this profile's cutoff under this profile's
        # grant, and the estate digest is the one that grant carries, not the caller's
        # storage policy.
        estate = admitted.grant.get("source_policy_digest")
        if (
            _observed(admitted.operation_context["cutoff_utc"], code)
            != _parse_timestamp(profile["observation_window_end"], code)
            or admitted.grant.get("grant_id") != profile["grant_id"]
            or estate != profile["source_estate_digest"]
        ):
            raise ValueError(code)
        return identity, estate, None

    return check_successful_bridge_result(
        value,
        plan=plan,
        plan_inputs=plan_inputs,
        capture_facts=capture_facts,
        creation_records=creation_records,
        creation_completed_at=creation_completed_at,
        artifact_attempt=artifact_attempt,
        stored_artifact=stored_artifact,
        execution_status=execution_status,
        native_metering=native_metering,
        native_jobs=native_jobs,
        clone_metadata=clone_metadata,
        principal=principal,
    )


def check_successful_bridge_result(
    value,
    *,
    plan,
    plan_inputs,
    capture_facts,
    creation_records,
    creation_completed_at,
    artifact_attempt,
    stored_artifact,
    execution_status,
    native_metering,
    native_jobs,
    clone_metadata,
    principal,
):
    """The result checks every capture route shares; only the principal differs.

    ``principal(profile)`` returns the one identity whose jobs may create the clones, the
    source estate digest of the authorizing grant, and the earliest instant a creation job
    may start, or None when the route binds no such instant. It raises ValueError when the
    route's authority does not bind this profile. ``clone_metadata`` maps each destination
    table to its tables.get resource, read back after the jobs finished.
    """
    code = "source_bridge_result_invalid"
    checked, raw = canonical_json_object(value, code)
    if (
        execution_status != "succeeded"
        or type(checked) is not dict
        or set(checked) != _FIELDS
        or checked["contract_version"] != "open_intelligence_protected_source_snapshot_v3"
        or checked["missing_checks"] != []
        or type(checked["creation_records"]) is not list
        or any(type(record) is not dict for record in checked["creation_records"])
        or type(checked["limitations"]) is not list
        or any(type(item) is not str or not item.strip() for item in checked["limitations"])
    ):
        raise ValueError(code)
    plan = validate_bridge_plan(plan, **plan_inputs)
    profile = plan["snapshot_plan"]
    if any(checked[key] != profile[key] for key in _PROFILE_RESULT_FIELDS) or any(
        checked[key] != plan[key] for key in ("cutoff_date", "client_scope_id", "market_scope")
    ):
        raise ValueError(code)
    if type(creation_records) is not list or len(creation_records) != len(
        profile["relation_bindings"]
    ):
        raise ValueError(code)
    for record, relation in zip(creation_records, profile["relation_bindings"], strict=True):
        if (
            type(record) is not dict
            or set(record) != _CREATION_FIELDS
            or any(record[key] != relation[key] for key in RELATION_FIELDS)
            or record["state"] != "succeeded"
            or type(record["job_id"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", record["job_id"]) is None
        ):
            raise ValueError(code)
        _digest(record["native_job_digest"], code)
    if canonical_bytes(checked["creation_records"]) != canonical_bytes(creation_records):
        raise ValueError(code)
    identity, estate, earliest = principal(profile)
    if (
        type(native_jobs) is not dict
        or type(creation_completed_at) is not dict
        or set(native_jobs) != {record["job_id"] for record in creation_records}
        or len(native_jobs) != len(creation_records)
    ):
        raise ValueError(code)
    for record, statement in zip(creation_records, plan["creation_statements"], strict=True):
        if statement["lane"] != record["lane"]:
            raise ValueError(code)
        _check_native_job(
            native_jobs[record["job_id"]],
            record=record,
            statement=statement,
            completed_at=creation_completed_at.get(record["lane"]),
            identity=identity,
            code=code,
        )
        if earliest is not None:
            _check_job_window(
                native_jobs[record["job_id"]],
                earliest=earliest,
                latest=_parse_timestamp(checked["captured_at"], code),
                code=code,
            )
    if type(artifact_attempt) is not dict or type(stored_artifact) is not dict:
        raise ValueError(code)
    if type(stored_artifact.get("size_bytes")) is not int:
        raise ValueError(code)
    _validate_attempt(artifact_attempt, _CAPTURE_VERSION_V2)
    _validate_stored(stored_artifact, artifact_attempt, _CAPTURE_VERSION_V2)
    for key, receipt in (
        ("artifact_attempt", artifact_attempt),
        ("stored_artifact", stored_artifact),
    ):
        if type(checked[key]) is not dict or canonical_bytes(checked[key]) != canonical_bytes(
            receipt
        ):
            raise ValueError(code)
    capture = validate_capture_facts(
        capture_facts,
        profile=profile,
        artifacts=plan_inputs["artifacts"],
        source_estate_digest=estate,
        creation_completed_at=creation_completed_at,
        stored_created_at=stored_artifact["created_at"],
        result_captured_at=checked["captured_at"],
        attempt_captured_at=artifact_attempt["captured_at"],
    )
    # Each recorded fingerprint is recomputed from the clone as read back, not checked for shape.
    windows = {}
    for record in creation_records:
        statistics = native_jobs[record["job_id"]]["statistics"]
        windows[record["lane"]] = (
            _millis_instant(statistics.get("creationTime"), code),
            _millis_instant(statistics["endTime"], code),
        )
    _check_clone_readbacks(
        profile["relation_bindings"], capture["relation_readbacks"], clone_metadata, windows, code
    )
    for field, content in (
        ("snapshot_plan_digest", plan),
        ("snapshot_digest", capture),
        ("capture_receipt_digest", creation_records),
    ):
        if checked[field] != canonical_digest(content):
            raise ValueError(code)
    if type(native_metering) is not dict or set(native_metering) != {
        "query_count",
        "total_bytes_billed",
    }:
        raise ValueError(code)
    for field in ("query_count", "total_bytes_billed"):
        if (
            type(checked[field]) is not int
            or type(native_metering[field]) is not int
            or native_metering[field] < 0
            or checked[field] != native_metering[field]
        ):
            raise ValueError(code)
    return json.loads(raw)
