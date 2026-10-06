"""Owner reconciliation of a consumed daily derivation that has no result.

A daily child that ends after consume and before record leaves a consumption the
store counts as in flight until a result row exists. Only the child identity can
record and cancel refuses a consumed derivation, so the owner closes it through
sp_reconcile_open_intelligence_daily_consumption_v1, which writes an
owner_reconciliation_hold tombstone. The hold keeps the reservation charged against
the grant's monthly allowance, releases the in flight credit cap and the unresolved
job check, and makes the consumption terminal: consume and record refuse it after.

The owner never types in how the execution ended. They supply the derivation, the
consumption, the execution name stored on the consumption row and when and as whom
they observed it, together with the bytes of the Cloud Run Admin API v2 Execution
resource they retained (GET https://run.googleapis.com/v2/{execution name}). This
module takes the execution name, terminal state and completion time from those bytes,
refuses an execution that has no completionTime, is still running or reconciling, or
has no terminal Completed condition, refuses a readback of any execution other than
the stored one or of another job, computes the readback digest itself, and only then
builds the canonical reconciliation record and renders the parameterised CALL. It
reads nothing and calls nothing; the owner runs the statement under their own
identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .daily_execution_contracts import (
    CONSUMPTION_ID,
    DERIVATION_ID,
    HEX64,
    DailyContractError,
    _instant,
    canonical_json_object,
)

ROUTINE = "sp_reconcile_open_intelligence_daily_consumption_v1"
CONTRACT_VERSION = "daily_consumption_reconciliation_v1"
# Cloud Run reports an execution complete in exactly one of these outcomes. Anything
# else (running, pending, unknown) is not terminal and cannot be reconciled.
TERMINAL_STATES = ("cancelled", "failed", "succeeded")
CONSUMPTION_RECONCILIATION_FIELDS = frozenset(
    {
        "child_job_resource",
        "consumption_id",
        "contract_version",
        "derivation_id",
        "execution_completed_at",
        "execution_name",
        "execution_readback_sha256",
        "execution_terminal_state",
        "observed_at",
        "observer_principal",
    }
)
# The fields the owner supplies. The rest of the record is derived from the readback.
RECONCILIATION_REQUEST_FIELDS = frozenset(
    {
        "child_job_resource",
        "consumption_id",
        "contract_version",
        "derivation_id",
        "execution_name",
        "observed_at",
        "observer_principal",
    }
)
PARAMETERS = (
    "derivation_id",
    "consumption_id",
    "execution_name",
    "execution_terminal_state",
    "canonical_reconciliation_json",
    "reconciliation_digest",
)
_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_DATASET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
_INVALID = "daily_consumption_reconciliation_invalid"
_READBACK_INVALID = "daily_consumption_reconciliation_readback_invalid"
_NOT_TERMINAL = "daily_consumption_reconciliation_execution_not_terminal"
_MISMATCH = "daily_consumption_reconciliation_execution_mismatch"
_EXECUTION_RESOURCE = re.compile(
    r"^(projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/jobs/[a-z0-9-]+)"
    r"/executions/[a-z0-9-]+$"
)
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
# The Completed condition of a v2 Execution: CONDITION_SUCCEEDED ends it succeeded;
# CONDITION_FAILED ends it cancelled when the execution reason is CANCELLED and failed
# otherwise. CANCELLING, PENDING, RECONCILING and anything unknown are not an end.
_COMPLETED_FAILED_REASONS = frozenset(
    {None, "EXECUTION_REASON_UNDEFINED", "NON_ZERO_EXIT_CODE", "JOB_STATUS_SERVICE_POLLING_ERROR"}
)


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def validate_consumption_reconciliation(value: object) -> tuple[dict[str, object], bytes]:
    record, raw = canonical_json_object(value, "daily_consumption_reconciliation_noncanonical")
    if set(record) != CONSUMPTION_RECONCILIATION_FIELDS:
        raise DailyContractError(_INVALID)
    if (
        record["contract_version"] != CONTRACT_VERSION
        or not isinstance(record["derivation_id"], str)
        or DERIVATION_ID.fullmatch(record["derivation_id"]) is None
        or not isinstance(record["consumption_id"], str)
        or CONSUMPTION_ID.fullmatch(record["consumption_id"]) is None
        or not _nonempty(record["execution_name"])
        or not _nonempty(record["child_job_resource"])
        or not _nonempty(record["observer_principal"])
        or record["execution_terminal_state"] not in TERMINAL_STATES
        or not isinstance(record["execution_readback_sha256"], str)
        or HEX64.fullmatch(record["execution_readback_sha256"]) is None
    ):
        raise DailyContractError(_INVALID)
    completed = _instant(record["execution_completed_at"], _INVALID)
    observed = _instant(record["observed_at"], _INVALID)
    if completed > observed:
        raise DailyContractError(_INVALID)
    return record, raw


def _refuse(code: str) -> None:
    raise DailyContractError(code)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _refuse(_READBACK_INVALID)
        result[key] = value
    return result


def _no_constant(_: str) -> object:
    raise DailyContractError(_READBACK_INVALID)


def _completion_time(value: object) -> str:
    if value is None or value == "":
        _refuse(_NOT_TERMINAL)
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        _refuse(_READBACK_INVALID)
    try:
        seconds = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError as error:
        raise DailyContractError(_READBACK_INVALID) from error
    # The store keeps microseconds; a finer completion time is truncated, never rounded
    # up past the moment Cloud Run reported.
    micros = (match.group(2) or "").ljust(6, "0")[:6]
    return seconds.strftime("%Y-%m-%dT%H:%M:%S") + f".{micros}Z"


def _terminal_state(execution: dict[str, object]) -> str:
    conditions = execution.get("conditions")
    if not isinstance(conditions, list) or any(not isinstance(item, dict) for item in conditions):
        _refuse(_NOT_TERMINAL)
    completed = [item for item in conditions if item.get("type") == "Completed"]
    if len(completed) != 1:
        _refuse(_NOT_TERMINAL)
    condition = completed[0]
    state = condition.get("state")
    reason = condition.get("executionReason")
    if state == "CONDITION_SUCCEEDED" and reason is None:
        return "succeeded"
    if state == "CONDITION_FAILED" and reason == "CANCELLED":
        return "cancelled"
    if state == "CONDITION_FAILED" and reason in _COMPLETED_FAILED_REASONS:
        return "failed"
    raise DailyContractError(_NOT_TERMINAL)


def execution_readback(readback: object) -> dict[str, str]:
    """Derive how a Cloud Run execution ended from its retained v2 readback bytes."""
    if not isinstance(readback, bytes):
        _refuse(_READBACK_INVALID)
    try:
        execution = json.loads(
            readback.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_no_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise DailyContractError(_READBACK_INVALID) from error
    if not isinstance(execution, dict):
        _refuse(_READBACK_INVALID)
    name = execution.get("name")
    match = _EXECUTION_RESOURCE.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        _refuse(_READBACK_INVALID)
    job_resource = match.group(1)
    job = execution.get("job")
    if job != job_resource and job != job_resource.rsplit("/", 1)[1]:
        _refuse(_MISMATCH)
    completed_at = _completion_time(execution.get("completionTime"))
    if execution.get("reconciling") not in (None, False) or execution.get("runningCount") not in (
        None,
        0,
    ):
        _refuse(_NOT_TERMINAL)
    return {
        "execution_name": name,
        "execution_terminal_state": _terminal_state(execution),
        "execution_completed_at": completed_at,
        "execution_readback_sha256": hashlib.sha256(readback).hexdigest(),
    }


def reconciliation_record(request: object, readback: object) -> dict[str, object]:
    """Build the canonical record from the owner's request and the execution readback."""
    fields, _ = canonical_json_object(request, "daily_consumption_reconciliation_noncanonical")
    if set(fields) != RECONCILIATION_REQUEST_FIELDS:
        _refuse(_INVALID)
    observed = execution_readback(readback)
    stored = fields["execution_name"]
    job = fields["child_job_resource"]
    if (
        observed["execution_name"] != stored
        or not isinstance(job, str)
        or not observed["execution_name"].startswith(job + "/executions/")
    ):
        _refuse(_MISMATCH)
    return {**fields, **observed}


def call_parameters(request: object, readback: object) -> tuple[tuple[str, str], ...]:
    record, raw = validate_consumption_reconciliation(reconciliation_record(request, readback))
    text = raw.decode("utf-8")
    values = {
        "derivation_id": record["derivation_id"],
        "consumption_id": record["consumption_id"],
        "execution_name": record["execution_name"],
        "execution_terminal_state": record["execution_terminal_state"],
        "canonical_reconciliation_json": text,
        "reconciliation_digest": hashlib.sha256(raw).hexdigest(),
    }
    return tuple((name, values[name]) for name in PARAMETERS)


def render_call(
    request: object, readback: object, *, project: str, dataset: str
) -> dict[str, object]:
    if (
        not isinstance(project, str)
        or _PROJECT.fullmatch(project) is None
        or not isinstance(dataset, str)
        or _DATASET.fullmatch(dataset) is None
    ):
        raise DailyContractError("daily_consumption_reconciliation_target_invalid")
    parameters = call_parameters(request, readback)
    statement = (
        f"CALL `{project}.{dataset}.{ROUTINE}`("
        + ", ".join(f"@{name}" for name, _ in parameters)
        + ")"
    )
    return {
        "statement": statement,
        "parameters": [
            {"name": name, "type": "STRING", "value": item} for name, item in parameters
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 4:
        print(
            "usage: daily_consumption_reconciliation <request.json> <execution_readback.json>"
            " <project> <dataset>",
            file=sys.stderr,
        )
        return 2
    request_path, readback_path, project, dataset = arguments
    try:
        rendered = render_call(
            Path(request_path).read_bytes(),
            Path(readback_path).read_bytes(),
            project=project,
            dataset=dataset,
        )
    except DailyContractError as error:
        print(error.code, file=sys.stderr)
        return 1
    print(json.dumps(rendered, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
