"""DailyStore over the evidence bucket.

One conditional control record per slot carries the lease owner, the fencing
epoch, the dispatch intent and the current stage references. Claim, publication
of a stage reference and lease release all compare-and-swap that record's
generation through an injectable object client. Stage records themselves are
immutable, create-only objects; they become current state only when the control
record references them, so a stale owner may leave an orphan artifact but can
publish nothing. A referenced record that cannot be read is an error, never an
absence, so unknown paid work stays unknown. Unit ledgers reuse the kernel's
``_validate_unit_ledger`` and ``resume_incomplete_units`` works over the store.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import (
    StageResult,
    _stage_result,
    _validate_unit_ledger,
)

EVIDENCE_BUCKET = "ogilvy-trends-v2-execution-approvals-staging"
CONTROL_CONTRACT = "42_daily_control_v1"
CONTROL_CONTRACT_V2 = "42_daily_control_v2"
SOURCE_ESTATE_V2 = "intelligence-42-core"
STAGES_V2 = ("collection", "exposure", "capture", "compose", "certify", "release")
_JOB_PREFIX = "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
_PARENT_EXECUTION_PREFIX = _JOB_PREFIX + "intelligence-42-daily-staging/executions/"
# Amendment d folded every daily operation onto the daily job under the orchestration
# identity, so every stage's child execution is an execution of that job. The registry
# binding is checked equal to this by daily_operation_map and its tests.
_DAILY_CHILD_JOB = "intelligence-42-daily-staging"
_JOBS_V2 = dict.fromkeys(STAGES_V2, _DAILY_CHILD_JOB)


def child_job_resource_v2(stage: object) -> str:
    """The child job resource a v2 stage's dispatch intent must name."""
    if stage not in _JOBS_V2:
        _refuse("stage_key_invalid")
    return _JOB_PREFIX + _JOBS_V2[stage]


_INTENT_V2_FIELDS = frozenset(
    {
        "intent_id",
        "business_attempt_id",
        "stage",
        "attempt_version",
        "input_digest",
        "operation_context_sha256",
        "authorizing_approval_id",
        "authorizing_grant_digest",
        "child_job_resource",
        "lease_owner",
        "lease_epoch",
        "phase",
        "derivation_id",
        "dispatch_observation_reference",
        "resolution_reference",
    }
)
STAGE_RECORD_CONTRACT = "42_daily_stage_record_v1"
SLOT_PREFIX = "42/daily/slots"
DEFAULT_LEASE_SECONDS = 3600
TIMELINE_FIELDS = (
    "observation_window_end",
    "collection_started_at",
    "collection_completed_at",
    "snapshot_as_of",
    "capture_available_at",
)
_STAGE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}(@[1-9][0-9]*)?\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_FIELDS = frozenset(
    {
        "contract_version",
        "operation_id",
        "cutoff_utc",
        "lease",
        "epoch",
        "dispatch_intent",
        "stages",
        "units",
        "timeline",
    }
)
_CONTROL_V2_FIELDS = frozenset(
    {
        "contract_version",
        "operation_id",
        "source_estate_id",
        "cutoff_utc",
        "lease",
        "epoch",
        "dispatch_intent",
        "stages",
        "units",
        "timeline",
        "legacy_migration_reference",
    }
)


class StoreRefusal(ValueError):
    """Refusals of the store; ``str(error)`` is the code."""


class PreconditionFailed(Exception):
    """The object client's generation precondition did not hold."""


class ObjectClient(Protocol):
    def read(self, name: str) -> tuple[bytes, int] | None: ...
    def write(self, name: str, payload: bytes, *, if_generation_match: int) -> int: ...


def _refuse(code: str) -> None:
    raise StoreRefusal(code)


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse(code)
    return value.astimezone(UTC)


def slot_operation_id(*, environment: str, source_policy_digest: str, cutoff_utc: datetime) -> str:
    """The global slot: environment, source estate and closed cutoff, nothing else."""
    if not isinstance(environment, str) or not environment:
        _refuse("slot_environment_invalid")
    if not isinstance(source_policy_digest, str) or _HEX_64.fullmatch(source_policy_digest) is None:
        _refuse("slot_source_policy_invalid")
    cutoff = _aware(cutoff_utc, "slot_cutoff_not_timezone_aware")
    payload = {
        "environment": environment,
        "source_policy_digest": source_policy_digest,
        "cutoff_utc": cutoff.isoformat(),
    }
    return sha256(canonical_bytes(payload)).hexdigest()


def slot_operation_id_v2(*, environment: str, source_estate_id: str, cutoff_utc: datetime) -> str:
    if environment != "staging":
        _refuse("slot_environment_invalid")
    if source_estate_id != SOURCE_ESTATE_V2:
        _refuse("slot_source_estate_invalid")
    cutoff = _aware(cutoff_utc, "slot_cutoff_not_timezone_aware")
    if cutoff.time() != time.min:
        _refuse("slot_cutoff_not_midnight")
    return sha256(
        canonical_bytes(
            {
                "environment": environment,
                "source_estate_id": source_estate_id,
                "cutoff_utc": cutoff.isoformat(),
            }
        )
    ).hexdigest()


def _v2_instant(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            _refuse("control_record_invalid")
    return _aware(value, "control_record_invalid")


def _v2_identifier(value: object, prefix: str = "") -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and _HEX_64.fullmatch(value[len(prefix) :]) is not None
    )


def _v2_owner(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_PARENT_EXECUTION_PREFIX)
        and re.fullmatch(r"[a-zA-Z0-9-]+", value[len(_PARENT_EXECUTION_PREFIX) :]) is not None
    )


def _validate_intent_v2(value: object, operation_id: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != _INTENT_V2_FIELDS:
        _refuse("dispatch_intent_invalid")
    stage, attempt = value["stage"], value["attempt_version"]
    if stage not in STAGES_V2 or type(attempt) is not int or attempt < 1:
        _refuse("dispatch_intent_invalid")
    expected_intent = (
        "dsi_"
        + sha256(
            canonical_bytes(
                {
                    "control_contract_version": CONTROL_CONTRACT_V2,
                    "slot_id": operation_id,
                    "stage": stage,
                    "attempt_version": attempt,
                }
            )
        ).hexdigest()
    )
    expected_attempt = (
        "bat_"
        + sha256(
            canonical_bytes(
                {
                    "operation_id": operation_id,
                    "attempt": f"{stage}@{attempt}",
                }
            )
        ).hexdigest()
    )
    if value["intent_id"] != expected_intent or value["business_attempt_id"] != expected_attempt:
        _refuse("dispatch_intent_invalid")
    if any(
        not _v2_identifier(value[field])
        for field in ("input_digest", "operation_context_sha256", "authorizing_grant_digest")
    ):
        _refuse("dispatch_intent_invalid")
    if (
        not _v2_identifier(value["authorizing_approval_id"], "exa_")
        or value["child_job_resource"] != _JOB_PREFIX + _JOBS_V2[stage]
    ):
        _refuse("dispatch_intent_invalid")
    if (
        not _v2_owner(value["lease_owner"])
        or type(value["lease_epoch"]) is not int
        or value["lease_epoch"] < 1
    ):
        _refuse("dispatch_intent_invalid")
    phase = value["phase"]
    if phase not in {"prepared", "derived", "dispatch_started", "dispatch_observed", "held"}:
        _refuse("dispatch_intent_invalid")
    if phase == "prepared":
        if any(
            value[key] is not None
            for key in ("derivation_id", "dispatch_observation_reference", "resolution_reference")
        ):
            _refuse("dispatch_intent_invalid")
    elif not _v2_identifier(value["derivation_id"], "exd_"):
        _refuse("dispatch_intent_invalid")
    for field in ("dispatch_observation_reference", "resolution_reference"):
        if value[field] is not None and (
            not isinstance(value[field], str) or not value[field].strip()
        ):
            _refuse("dispatch_intent_invalid")
    return dict(value)


def unit_request_id_v2(request: Mapping[str, object]) -> str:
    if not isinstance(request, Mapping):
        _refuse("unit_request_invalid")
    for field in ("route", "subject"):
        if not isinstance(request.get(field), str) or not request[field].strip():
            _refuse("unit_request_invalid")
    if request.get("market") not in {"za", "ng", "ke"} or not _v2_identifier(
        request.get("request_sha256")
    ):
        _refuse("unit_request_invalid")
    start, end = _v2_instant(request.get("window_start")), _v2_instant(request.get("window_end"))
    if start >= end:
        _refuse("unit_request_invalid")
    return sha256(
        canonical_bytes(
            {
                "source_estate_id": SOURCE_ESTATE_V2,
                "route": request["route"],
                "market": request["market"],
                "subject": request["subject"],
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "request_sha256": request["request_sha256"],
            }
        )
    ).hexdigest()


def _unit_decimal(value):
    if not isinstance(value, str):
        _refuse("unit_credits_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        _refuse("unit_credits_invalid")
    if not amount.is_finite() or amount < 0 or format(amount.normalize(), "f") != value:
        _refuse("unit_credits_invalid")
    return amount


def _unit_intent_v2(control, context, derivation_id):
    pending = control["dispatch_intent"]
    if (
        pending is None
        or pending["stage"] != "collection"
        or pending["phase"] not in {"dispatch_started", "dispatch_observed", "held"}
        or pending["derivation_id"] != derivation_id
        or context.get("slot_id") != control["operation_id"]
        or context.get("stage") != "collection"
        or any(
            context.get(field) != pending[field]
            for field in (
                "lease_owner",
                "lease_epoch",
                "authorizing_grant_digest",
                "child_job_resource",
            )
        )
    ):
        _refuse("unit_intent_mismatch")


def _unit_permit_state_v2(control, permit, *, context, derivation_id, consumption_id, max_credits):
    _unit_intent_v2(control, context, derivation_id)
    required = {
        "contract_version",
        "operation_id",
        "consumption_id",
        "unit_id",
        "route",
        "market",
        "subject",
        "window_start",
        "window_end",
        "request_sha256",
        "quoted_credits",
        "max_calls",
        "permit_sequence",
        "created_at",
    }
    if (
        not isinstance(permit, Mapping)
        or set(permit) != required
        or permit["contract_version"] != "daily_collection_unit_permit_v1"
        or permit["operation_id"] != control["operation_id"]
        or permit["consumption_id"] != consumption_id
        or not _v2_identifier(consumption_id, "exc_")
        or type(permit["max_calls"]) is not int
        or permit["max_calls"] != 1
        or type(permit["permit_sequence"]) is not int
        or permit["permit_sequence"] < 1
        or type(max_credits) is not int
        or max_credits < 0
    ):
        _refuse("unit_permit_invalid")
    unit_id = unit_request_id_v2(permit)
    if permit["unit_id"] != unit_id:
        _refuse("unit_permit_invalid")
    _v2_instant(permit["created_at"])
    if unit_id in control["units"]:
        _refuse("unit_already_started")
    quote = _unit_decimal(permit["quoted_credits"])
    reserved = sum(
        (_unit_decimal(entry["quoted_credits"]) for entry in control["units"].values()), Decimal(0)
    )
    if reserved + quote > max_credits:
        _refuse("unit_credit_cap_exceeded")
    updated = json.loads(canonical_bytes(control))
    record = dict(permit)
    name = f"{SLOT_PREFIX}/{control['operation_id']}/units/{unit_id}/permit.json"
    updated["units"][unit_id] = {
        "permit_reference": name,
        "permit_digest": sha256(canonical_bytes(record)).hexdigest(),
        "consumption_id": consumption_id,
        "state": "attempt_started",
        "result_reference": None,
        "result_digest": None,
        "quoted_credits": permit["quoted_credits"],
        "permit_sequence": permit["permit_sequence"],
    }
    return updated, record


def _unit_result_v2(result, permit):
    required = {
        "contract_version",
        "operation_id",
        "consumption_id",
        "unit_id",
        "permit_sha256",
        "state",
        "calls",
        "charged_credits",
        "response_sha256",
        "completed_at",
        "reason",
    }
    if (
        not isinstance(result, Mapping)
        or set(result) != required
        or result["contract_version"] != "daily_collection_unit_result_v1"
        or any(result[key] != permit[key] for key in ("operation_id", "consumption_id", "unit_id"))
        or result["permit_sha256"] != sha256(canonical_bytes(permit)).hexdigest()
        or result["state"] not in {"complete", "empty", "refused", "unknown"}
        or type(result["calls"]) is not int
        or not 0 <= result["calls"] <= 1
        or _v2_instant(result["completed_at"]) < _v2_instant(permit["created_at"])
    ):
        _refuse("unit_result_invalid")
    if result["response_sha256"] is not None and not _v2_identifier(result["response_sha256"]):
        _refuse("unit_result_invalid")
    if result["reason"] is not None and (
        not isinstance(result["reason"], str) or not result["reason"].strip()
    ):
        _refuse("unit_result_invalid")
    checked = dict(result)
    if result["charged_credits"] is None:
        checked["state"] = "unknown"
        checked["reason"] = "unit_charge_unknown"
    elif _unit_decimal(result["charged_credits"]) > _unit_decimal(permit["quoted_credits"]):
        checked["state"] = "unknown"
        checked["reason"] = "unit_quote_exceeded"
    if checked["state"] in {"complete", "empty"} and (
        checked["calls"] != 1 or checked["response_sha256"] is None
    ):
        _refuse("unit_result_invalid")
    return checked


def _unpack(raw: bytes, code: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _refuse(code)
    if not isinstance(value, dict):
        _refuse(code)
    return value


class GcsObjectClient:
    """Native adapter: generation preconditions on one bucket. Unproven here."""

    def __init__(self, bucket):
        if getattr(bucket, "name", None) != EVIDENCE_BUCKET:
            _refuse("evidence_bucket_mismatch")
        self._bucket = bucket

    def read(self, name: str) -> tuple[bytes, int] | None:
        blob = self._bucket.get_blob(name, retry=None)
        if blob is None:
            return None
        generation = int(blob.generation)
        return blob.download_as_bytes(if_generation_match=generation, retry=None), generation

    def write(self, name: str, payload: bytes, *, if_generation_match: int) -> int:
        from google.api_core import exceptions

        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=if_generation_match,
                retry=None,
            )
        except exceptions.PreconditionFailed as error:
            raise PreconditionFailed(name) from error
        return int(blob.generation)


class DailyStore:
    def __init__(
        self,
        client: ObjectClient,
        *,
        owner: str,
        now: Callable[[], datetime],
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        prefix: str = SLOT_PREFIX,
    ):
        if not isinstance(owner, str) or not owner:
            _refuse("owner_invalid")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds <= 0
        ):
            _refuse("lease_seconds_invalid")
        self._client = client
        self._owner = owner
        self._now = now
        self._lease_seconds = lease_seconds
        self._prefix = prefix
        self._held: dict[str, tuple[int, int]] = {}
        self._held_v2: dict[str, tuple[int, int]] = {}
        self._timeline: dict[str, dict] = {}

    def _control_name(self, operation_id: str) -> str:
        return f"{self._prefix}/{operation_id}/control.json"

    def _record_name(self, operation_id: str, stage: str, epoch: int, attempt: int) -> str:
        return f"{self._prefix}/{operation_id}/stages/{stage}/{epoch}-{attempt}.json"

    def _read_control(self, operation_id: str) -> tuple[dict, int] | None:
        if not isinstance(operation_id, str) or _HEX_64.fullmatch(operation_id) is None:
            _refuse("operation_id_invalid")
        found = self._client.read(self._control_name(operation_id))
        if found is None:
            return None
        raw, generation = found
        control = _unpack(raw, "control_record_invalid")
        if (
            set(control) != _CONTROL_FIELDS
            or control["contract_version"] != CONTROL_CONTRACT
            or control["operation_id"] != operation_id
        ):
            _refuse("control_record_invalid")
        return control, generation

    def _read_control_v2(self, operation_id: str) -> tuple[dict, int] | None:
        if self._prefix != SLOT_PREFIX:
            _refuse("control_prefix_invalid")
        if not _v2_identifier(operation_id):
            _refuse("operation_id_invalid")
        found = self._client.read(self._control_name(operation_id))
        if found is None:
            return None
        raw, generation = found
        control = _unpack(raw, "control_record_invalid")
        if (
            set(control) != _CONTROL_V2_FIELDS
            or control["contract_version"] != CONTROL_CONTRACT_V2
            or control["operation_id"] != operation_id
        ):
            _refuse("control_record_invalid")
        try:
            if raw != canonical_bytes(control):
                _refuse("control_record_invalid")
        except (TypeError, ValueError):
            _refuse("control_record_invalid")
        if (
            operation_id
            != slot_operation_id_v2(
                environment="staging",
                source_estate_id=control["source_estate_id"],
                cutoff_utc=_v2_instant(control["cutoff_utc"]),
            )
            or type(control["epoch"]) is not int
            or control["epoch"] < 1
        ):
            _refuse("control_record_invalid")
        if any(type(control[key]) is not dict for key in ("stages", "units", "timeline")):
            _refuse("control_record_invalid")
        for stage, entry in control["stages"].items():
            if (
                stage not in STAGES_V2
                or type(entry) is not dict
                or set(entry)
                != {
                    "reference",
                    "record_digest",
                    "attempt",
                    "epoch",
                    "state",
                    "protected_result_id",
                    "protected_result_digest",
                }
                or any(type(entry[k]) is not int or entry[k] < 1 for k in ("attempt", "epoch"))
                or entry["epoch"] > control["epoch"]
                or type(entry["state"]) is not str
                or entry["state"] not in {"succeeded", "failed"}
                or not _v2_identifier(entry["record_digest"])
                or not _v2_identifier(entry["protected_result_digest"])
                or not _v2_identifier(entry["protected_result_id"], "exr_")
                or entry["protected_result_id"] != "exr_" + entry["protected_result_digest"]
                or entry["reference"]
                != self._record_name(operation_id, stage, entry["epoch"], entry["attempt"])
            ):
                _refuse("control_record_invalid")
        for unit_id, entry in control["units"].items():
            if (
                not _v2_identifier(unit_id)
                or type(entry) is not dict
                or set(entry)
                != {
                    "permit_reference",
                    "permit_digest",
                    "consumption_id",
                    "state",
                    "result_reference",
                    "result_digest",
                    "quoted_credits",
                    "permit_sequence",
                }
                or not _v2_identifier(entry["permit_digest"])
                or not _v2_identifier(entry["consumption_id"], "exc_")
                or type(entry["permit_sequence"]) is not int
                or entry["permit_sequence"] < 1
                or type(entry["state"]) is not str
                or entry["state"]
                not in {"attempt_started", "complete", "empty", "refused", "unknown"}
                or entry["permit_reference"]
                != f"{self._prefix}/{operation_id}/units/{unit_id}/permit.json"
            ):
                _refuse("control_record_invalid")
            _unit_decimal(entry["quoted_credits"])
            if entry["state"] == "attempt_started":
                if entry["result_reference"] is not None or entry["result_digest"] is not None:
                    _refuse("control_record_invalid")
            elif (
                not _v2_identifier(entry["result_digest"])
                or entry["result_reference"]
                != f"{self._prefix}/{operation_id}/units/{unit_id}/result.json"
            ):
                _refuse("control_record_invalid")
        if not set(control["timeline"]) <= set(TIMELINE_FIELDS):
            _refuse("control_record_invalid")
        for instant in control["timeline"].values():
            if instant is not None:
                _v2_instant(instant)
        lease = control["lease"]
        if lease is not None:
            if (
                type(lease) is not dict
                or set(lease) != {"owner", "epoch", "expires_at"}
                or not _v2_owner(lease["owner"])
                or type(lease["epoch"]) is not int
                or lease["epoch"] != control["epoch"]
            ):
                _refuse("control_record_invalid")
            _v2_instant(lease["expires_at"])
        migration = control["legacy_migration_reference"]
        if migration is not None and (not isinstance(migration, str) or not migration.strip()):
            _refuse("control_record_invalid")
        if control["dispatch_intent"] is not None:
            pending = _validate_intent_v2(control["dispatch_intent"], operation_id)
            if pending["lease_epoch"] != control["epoch"] or (
                lease is not None and pending["lease_owner"] != lease["owner"]
            ):
                _refuse("control_record_invalid")
        return control, generation

    def _write_control(self, operation_id: str, control: dict, generation: int) -> int:
        return self._client.write(
            self._control_name(operation_id),
            canonical_bytes(control),
            if_generation_match=generation,
        )

    def _lease_live(self, control: dict, now: datetime) -> bool:
        lease = control["lease"]
        if lease is None:
            return False
        return datetime.fromisoformat(lease["expires_at"]) > now

    def claim(self, operation_id: str, cutoff_utc: datetime) -> bool:
        cutoff = _aware(cutoff_utc, "cutoff_not_timezone_aware")
        now = _aware(self._now(), "now_not_timezone_aware")
        found = self._read_control(operation_id)
        expires = (now + timedelta(seconds=self._lease_seconds)).isoformat()
        if found is None:
            epoch, generation = 1, 0
            control = {
                "contract_version": CONTROL_CONTRACT,
                "operation_id": operation_id,
                "cutoff_utc": cutoff.isoformat(),
                "lease": {"owner": self._owner, "epoch": epoch, "expires_at": expires},
                "epoch": epoch,
                "dispatch_intent": None,
                "stages": {},
                "units": {},
                "timeline": {},
            }
        else:
            control, generation = found
            if control["cutoff_utc"] != cutoff.isoformat():
                _refuse("slot_cutoff_conflict")
            if self._lease_live(control, now):
                return False
            epoch = control["epoch"] + 1
            control["lease"] = {"owner": self._owner, "epoch": epoch, "expires_at": expires}
            control["epoch"] = epoch
        try:
            new_generation = self._write_control(operation_id, control, generation)
        except PreconditionFailed:
            return False
        self._held[operation_id] = (epoch, new_generation)
        return True

    def claim_v2(
        self, operation_id: str, environment: str, source_estate_id: str, cutoff_utc: datetime
    ) -> bool:
        cutoff = _aware(cutoff_utc, "cutoff_not_timezone_aware")
        if cutoff.time() != time.min:
            _refuse("slot_cutoff_not_midnight")
        if not _v2_owner(self._owner):
            _refuse("owner_invalid")
        if operation_id != slot_operation_id_v2(
            environment=environment, source_estate_id=source_estate_id, cutoff_utc=cutoff
        ):
            _refuse("slot_operation_id_invalid")
        now = _aware(self._now(), "now_not_timezone_aware")
        if cutoff > now:
            _refuse("cutoff_window_open")
        expires = (now + timedelta(seconds=self._lease_seconds)).isoformat()
        found = self._read_control_v2(operation_id)
        if found is None:
            epoch, generation = 1, 0
            control = {
                "contract_version": CONTROL_CONTRACT_V2,
                "operation_id": operation_id,
                "source_estate_id": source_estate_id,
                "cutoff_utc": cutoff.isoformat(),
                "lease": {"owner": self._owner, "epoch": epoch, "expires_at": expires},
                "epoch": epoch,
                "dispatch_intent": None,
                "stages": {},
                "units": {},
                "timeline": {},
                "legacy_migration_reference": None,
            }
        else:
            control, generation = found
            if (
                control["source_estate_id"] != source_estate_id
                or control["cutoff_utc"] != cutoff.isoformat()
            ):
                _refuse("slot_cutoff_conflict")
            if self._lease_live(control, now) or control["dispatch_intent"] is not None:
                return False
            epoch = control["epoch"] + 1
            control["lease"] = {"owner": self._owner, "epoch": epoch, "expires_at": expires}
            control["epoch"] = epoch
        try:
            generation = self._client.write(
                self._control_name(operation_id),
                canonical_bytes(control),
                if_generation_match=generation,
            )
        except PreconditionFailed:
            return False
        self._held_v2[operation_id] = (epoch, generation)
        return True

    def _fenced_control_v2(self, operation_id: str) -> tuple[dict, int, int]:
        held = self._held_v2.get(operation_id)
        if held is None:
            _refuse("lease_not_held")
        found = self._read_control_v2(operation_id)
        if found is None:
            _refuse("control_record_missing")
        control, generation = found
        lease = control["lease"]
        if (
            lease is None
            or lease["owner"] != self._owner
            or lease["epoch"] != held[0]
            or not self._lease_live(control, _aware(self._now(), "now_not_timezone_aware"))
        ):
            _refuse("stale_owner")
        return control, generation, held[0]

    def _commit_control_v2(self, operation_id: str, control: dict, generation: int) -> None:
        try:
            next_generation = self._write_control(operation_id, control, generation)
        except PreconditionFailed:
            _refuse("stale_owner")
        if operation_id in self._held_v2:
            self._held_v2[operation_id] = (control["epoch"], next_generation)

    def current_lease_v2(self, operation_id: str) -> dict:
        control, _generation, _epoch = self._fenced_control_v2(operation_id)
        return dict(control["lease"])

    def renew_claim_v2(self, operation_id: str) -> None:
        control, generation, _epoch = self._fenced_control_v2(operation_id)
        control["lease"]["expires_at"] = (
            _aware(self._now(), "now_not_timezone_aware") + timedelta(seconds=self._lease_seconds)
        ).isoformat()
        self._commit_control_v2(operation_id, control, generation)

    def read_intent_v2(self, operation_id: str) -> dict | None:
        found = self._read_control_v2(operation_id)
        return (
            None
            if found is None or found[0]["dispatch_intent"] is None
            else dict(found[0]["dispatch_intent"])
        )

    @staticmethod
    def _read_chain_v2(derivation_id, clients):
        if not _v2_identifier(derivation_id, "exd_") or clients is None:
            _refuse("stage_authority_unavailable")
        try:
            from src.analysis.open_intelligence.daily_execution_authority import (
                read_daily_execution_chain,
            )
        except ImportError as error:
            raise StoreRefusal("stage_authority_module_unavailable") from error
        return read_daily_execution_chain(derivation_id=derivation_id, clients=clients)

    @staticmethod
    def _chain_result_v2(operation_id, stage, chain):
        context, result = chain.operation_context, chain.result
        if context["slot_id"] != operation_id or context["stage"] != stage:
            _refuse("stage_authority_context_mismatch")
        try:
            raw = result["canonical_result_json"]
            envelope = json.loads(raw)
        except (KeyError, TypeError, ValueError) as error:
            raise StoreRefusal("stage_authority_result_invalid") from error
        if (
            canonical_bytes(envelope).decode("utf-8") != raw
            or sha256(raw.encode("utf-8")).hexdigest() != result["result_digest"]
            or not _v2_identifier(result["result_id"], "exr_")
            or envelope["operation_context_sha256"] != chain.derivation["operation_context_sha256"]
        ):
            _refuse("stage_authority_result_invalid")
        state = envelope["terminal_state"]
        if state not in {"succeeded", "failed"}:
            _refuse("stage_authority_result_invalid")
        if (
            envelope["effect_state"] == "unknown"
            or envelope["spend_state"] == "unknown"
            or envelope["stage_metering"]["complete"] is not True
        ):
            state = "unknown"
        safe = envelope["effect_state"] == "no_effect" and envelope["spend_state"] == "no_spend"
        return {
            "state": state,
            "input_digest": context["input_digest"],
            "output_digest": result["result_digest"],
            "result_reference": result["result_id"],
            "retry_safe": state == "failed" and safe,
        }

    def _stage_record_v2(self, operation_id, stage):
        if stage not in STAGES_V2:
            _refuse("stage_key_invalid")
        found = self._read_control_v2(operation_id)
        if found is None or stage not in found[0]["stages"]:
            return None
        entry = found[0]["stages"][stage]
        if not isinstance(entry, dict) or set(entry) != {
            "reference",
            "record_digest",
            "attempt",
            "epoch",
            "state",
            "protected_result_id",
            "protected_result_digest",
        }:
            _refuse("stage_record_invalid")
        if any(type(entry[key]) is not int or entry[key] < 1 for key in ("attempt", "epoch")):
            _refuse("stage_record_invalid")
        expected = self._record_name(operation_id, stage, entry["epoch"], entry["attempt"])
        if entry["reference"] != expected or not _v2_identifier(entry["record_digest"]):
            _refuse("stage_record_invalid")
        stored = self._client.read(expected)
        if stored is None:
            _refuse("stage_record_missing")
        raw, _generation = stored
        record = _unpack(raw, "stage_record_invalid")
        if (
            raw != canonical_bytes(record)
            or sha256(raw).hexdigest() != entry["record_digest"]
            or set(record)
            != {
                "contract_version",
                "operation_id",
                "stage",
                "attempt",
                "epoch",
                "recorded_at",
                "derivation_id",
                "result",
                "protected_result_id",
                "protected_result_digest",
            }
            or record["contract_version"] != "42_daily_stage_record_v2"
            or record["operation_id"] != operation_id
            or record["stage"] != stage
            or record["epoch"] != entry["epoch"]
            or record["attempt"] != entry["attempt"]
            or record["protected_result_id"] != entry["protected_result_id"]
            or record["protected_result_digest"] != entry["protected_result_digest"]
            or record["result"]["state"] != entry["state"]
        ):
            _refuse("stage_record_invalid")
        _v2_instant(record["recorded_at"])
        return record

    def read_stage_v2(self, operation_id, stage, *, clients):
        record = self._stage_record_v2(operation_id, stage)
        if record is None:
            return None
        chain = self._read_chain_v2(record["derivation_id"], clients)
        result = self._chain_result_v2(operation_id, stage, chain)
        if (
            result != record["result"]
            or result["result_reference"] != record["protected_result_id"]
            or result["output_digest"] != record["protected_result_digest"]
            or chain.operation_context["lease_epoch"] != record["epoch"]
        ):
            _refuse("stage_record_authority_mismatch")
        return result

    def publish_terminal_v2(self, operation_id, stage, derivation_id, *, clients, reconcile=False):
        chain = self._read_chain_v2(derivation_id, clients)
        result = self._chain_result_v2(operation_id, stage, chain)
        if result["state"] == "unknown":
            return result
        if reconcile:
            found = self._read_control_v2(operation_id)
            if found is None:
                _refuse("control_record_missing")
            control, generation = found
            epoch = control["epoch"]
        else:
            control, generation, epoch = self._fenced_control_v2(operation_id)
        pending = control["dispatch_intent"]
        if (
            pending is None
            or pending["stage"] != stage
            or pending["derivation_id"] != derivation_id
            or pending["operation_context_sha256"] != chain.derivation["operation_context_sha256"]
            or pending["input_digest"] != result["input_digest"]
            or pending["lease_epoch"] != chain.operation_context["lease_epoch"]
            or pending["lease_owner"] != chain.operation_context["lease_owner"]
            or pending["authorizing_grant_digest"]
            != chain.operation_context["authorizing_grant_digest"]
        ):
            _refuse("stage_authority_context_mismatch")
        record = {
            "contract_version": "42_daily_stage_record_v2",
            "operation_id": operation_id,
            "stage": stage,
            "attempt": pending["attempt_version"],
            "epoch": epoch,
            "recorded_at": _aware(self._now(), "now_not_timezone_aware").isoformat(),
            "derivation_id": derivation_id,
            "result": result,
            "protected_result_id": result["result_reference"],
            "protected_result_digest": result["output_digest"],
        }
        name = self._record_name(operation_id, stage, epoch, pending["attempt_version"])
        existing = self._client.read(name)
        if existing is not None:
            previous = _unpack(existing[0], "stage_record_conflict")
            record["recorded_at"] = previous.get("recorded_at")
            _v2_instant(record["recorded_at"])
            if existing[0] != canonical_bytes(record):
                _refuse("stage_record_conflict")
        else:
            try:
                self._client.write(name, canonical_bytes(record), if_generation_match=0)
            except PreconditionFailed:
                _refuse("stage_record_conflict")
        control["stages"][stage] = {
            "reference": name,
            "record_digest": sha256(canonical_bytes(record)).hexdigest(),
            "attempt": record["attempt"],
            "epoch": epoch,
            "state": result["state"],
            "protected_result_id": result["result_reference"],
            "protected_result_digest": result["output_digest"],
        }
        control["dispatch_intent"] = None
        self._commit_control_v2(operation_id, control, generation)
        return result

    def _consumed_unit_control_v2(self, operation_id, consumed_authority):
        try:
            from src.analysis.open_intelligence.daily_execution_authority import (
                require_daily_consumption,
            )
        except ImportError as error:
            raise StoreRefusal("stage_authority_module_unavailable") from error
        consumed = require_daily_consumption(
            consumed_authority, operation="daily_source_collection"
        )
        if self._owner != consumed.execution_name:
            _refuse("unit_writer_identity_mismatch")
        found = self._read_control_v2(operation_id)
        if found is None:
            _refuse("control_record_missing")
        control, generation = found
        _unit_intent_v2(control, consumed.operation_context, consumed.derivation_id)
        if (
            control["dispatch_intent"]["operation_context_sha256"]
            != consumed.operation_context_sha256
        ):
            _refuse("unit_intent_mismatch")
        return consumed, control, generation

    def publish_unit_permit_v2(self, operation_id, permit, *, consumed_authority):
        consumed, control, generation = self._consumed_unit_control_v2(
            operation_id, consumed_authority
        )
        if not isinstance(permit, Mapping) or not _v2_identifier(permit.get("unit_id")):
            _refuse("unit_permit_invalid")
        if permit["unit_id"] in control["units"]:
            _refuse("unit_already_started")
        if any(
            entry["consumption_id"] == consumed.consumption_id
            and entry["state"] in {"attempt_started", "unknown"}
            for entry in control["units"].values()
        ):
            _refuse("unit_state_unknown")
        record = dict(permit)
        name = f"{self._prefix}/{operation_id}/units/{permit['unit_id']}/permit.json"
        orphan = self._client.read(name)
        if orphan is not None:
            previous = _unpack(orphan[0], "unit_permit_conflict")
            record["created_at"] = previous.get("created_at")
            if orphan[0] != canonical_bytes(record):
                _refuse("unit_permit_conflict")
        else:
            record["created_at"] = _aware(self._now(), "now_not_timezone_aware").isoformat()
        if _v2_instant(record.get("created_at")) > _aware(self._now(), "now_not_timezone_aware"):
            _refuse("unit_permit_invalid")
        updated, record = _unit_permit_state_v2(
            control,
            record,
            context=consumed.operation_context,
            derivation_id=consumed.derivation_id,
            consumption_id=consumed.consumption_id,
            max_credits=consumed.manifest["limits"]["max_credits"],
        )
        updated["units"][record["unit_id"]]["permit_reference"] = name
        if orphan is None:
            try:
                self._client.write(name, canonical_bytes(record), if_generation_match=0)
            except PreconditionFailed:
                _refuse("unit_permit_conflict")
        self._commit_control_v2(operation_id, updated, generation)
        _consumed, readback, _generation = self._consumed_unit_control_v2(
            operation_id, consumed_authority
        )
        if readback["units"].get(record["unit_id"]) != updated["units"][record["unit_id"]]:
            _refuse("unit_publication_unknown")
        return dict(record)

    def read_unit_v2(self, operation_id, unit_id, *, consumed_authority):
        consumed, control, _generation = self._consumed_unit_control_v2(
            operation_id, consumed_authority
        )
        entry = control["units"].get(unit_id)
        if entry is None:
            return None
        if entry["consumption_id"] != consumed.consumption_id:
            _refuse("unit_consumption_mismatch")
        expected = f"{self._prefix}/{operation_id}/units/{unit_id}/permit.json"
        if entry["permit_reference"] != expected:
            _refuse("unit_reference_invalid")
        found = self._client.read(expected)
        if found is None or sha256(found[0]).hexdigest() != entry["permit_digest"]:
            _refuse("unit_permit_unavailable")
        permit = _unpack(found[0], "unit_permit_invalid")
        if canonical_bytes(permit) != found[0] or permit.get("unit_id") != unit_id:
            _refuse("unit_permit_invalid")
        result = None
        if entry["result_reference"] is not None:
            expected_result = f"{self._prefix}/{operation_id}/units/{unit_id}/result.json"
            if entry["result_reference"] != expected_result:
                _refuse("unit_reference_invalid")
            stored = self._client.read(expected_result)
            if stored is None or sha256(stored[0]).hexdigest() != entry["result_digest"]:
                _refuse("unit_result_unavailable")
            result = _unit_result_v2(_unpack(stored[0], "unit_result_invalid"), permit)
            if canonical_bytes(result) != stored[0] or result["state"] != entry["state"]:
                _refuse("unit_result_invalid")
        return {"permit": permit, "result": result, "state": entry["state"]}

    def publish_unit_result_v2(self, operation_id, result, *, consumed_authority):
        consumed, control, generation = self._consumed_unit_control_v2(
            operation_id, consumed_authority
        )
        unit_id = result.get("unit_id") if isinstance(result, Mapping) else None
        if not _v2_identifier(unit_id) or unit_id not in control["units"]:
            _refuse("unit_result_unbound")
        current = self.read_unit_v2(operation_id, unit_id, consumed_authority=consumed_authority)
        checked = _unit_result_v2(result, current["permit"])
        if _v2_instant(checked["completed_at"]) > _aware(self._now(), "now_not_timezone_aware"):
            _refuse("unit_result_invalid")
        if current["result"] is not None:
            if checked != current["result"]:
                _refuse("unit_result_conflict")
            return checked
        name = f"{self._prefix}/{operation_id}/units/{unit_id}/result.json"
        payload = canonical_bytes(checked)
        existing = self._client.read(name)
        if existing is None:
            try:
                self._client.write(name, payload, if_generation_match=0)
            except PreconditionFailed:
                _refuse("unit_result_conflict")
        elif existing[0] != payload:
            _refuse("unit_result_conflict")
        entry = control["units"][unit_id]
        if entry["consumption_id"] != consumed.consumption_id:
            _refuse("unit_consumption_mismatch")
        entry.update(
            result_reference=name, result_digest=sha256(payload).hexdigest(), state=checked["state"]
        )
        self._commit_control_v2(operation_id, control, generation)
        verified = self.read_unit_v2(operation_id, unit_id, consumed_authority=consumed_authority)
        if verified["result"] != checked:
            _refuse("unit_publication_unknown")
        return checked

    def prepare_intent_v2(self, operation_id: str, intent: Mapping[str, object]) -> None:
        control, generation, epoch = self._fenced_control_v2(operation_id)
        if control["dispatch_intent"] is not None:
            _refuse("dispatch_intent_unresolved")
        checked = _validate_intent_v2(intent, operation_id)
        if checked["lease_owner"] != self._owner or checked["lease_epoch"] != epoch:
            _refuse("dispatch_intent_invalid")
        if checked["phase"] != "prepared":
            _refuse("dispatch_intent_invalid")
        control["dispatch_intent"] = checked
        self._commit_control_v2(operation_id, control, generation)

    def bind_derivation_v2(self, operation_id: str, derivation: Mapping[str, object]) -> None:
        control, generation, _epoch = self._fenced_control_v2(operation_id)
        pending = control["dispatch_intent"]
        if (
            pending is None
            or pending["phase"] != "prepared"
            or not isinstance(derivation, Mapping)
            or not _v2_identifier(derivation.get("derivation_id"), "exd_")
            or derivation.get("operation_context_sha256") != pending["operation_context_sha256"]
        ):
            _refuse("derivation_binding_invalid")
        pending["derivation_id"] = derivation["derivation_id"]
        pending["phase"] = "derived"
        self._commit_control_v2(operation_id, control, generation)

    def mark_dispatch_started_v2(self, operation_id: str) -> None:
        control, generation, _epoch = self._fenced_control_v2(operation_id)
        pending = control["dispatch_intent"]
        if pending is None or pending["phase"] != "derived":
            _refuse("dispatch_already_started_or_unbound")
        pending["phase"] = "dispatch_started"
        self._commit_control_v2(operation_id, control, generation)

    def bind_dispatch_response_v2(self, operation_id: str, reference: str) -> None:
        control, generation, _epoch = self._fenced_control_v2(operation_id)
        pending = control["dispatch_intent"]
        if (
            pending is None
            or pending["phase"] != "dispatch_started"
            or not isinstance(reference, str)
            or not reference.strip()
        ):
            _refuse("dispatch_response_invalid")
        pending["dispatch_observation_reference"] = reference
        pending["phase"] = "dispatch_observed"
        self._commit_control_v2(operation_id, control, generation)

    def release_claim_v2(self, operation_id: str) -> None:
        held = self._held_v2.pop(operation_id, None)
        if held is None:
            return
        found = self._read_control_v2(operation_id)
        if found is None:
            return
        control, generation = found
        lease = control["lease"]
        if lease is None or lease["owner"] != self._owner or lease["epoch"] != held[0]:
            return
        control["lease"] = None
        try:
            self._client.write(
                self._control_name(operation_id),
                canonical_bytes(control),
                if_generation_match=generation,
            )
        except PreconditionFailed:
            return

    def _fenced_control(self, operation_id: str) -> tuple[dict, int, int]:
        held = self._held.get(operation_id)
        if held is None:
            _refuse("lease_not_held")
        epoch, _generation = held
        found = self._read_control(operation_id)
        if found is None:
            _refuse("control_record_missing")
        control, generation = found
        lease = control["lease"]
        if lease is None or lease["owner"] != self._owner or lease["epoch"] != epoch:
            _refuse("stale_owner")
        return control, generation, epoch

    def current_lease(self, operation_id: str) -> dict | None:
        """The live lease this instance holds, re-read from the control record."""
        held = self._held.get(operation_id)
        if held is None:
            return None
        found = self._read_control(operation_id)
        if found is None:
            return None
        control, _generation = found
        lease = control["lease"]
        if lease is None or lease["owner"] != self._owner or lease["epoch"] != held[0]:
            return None
        if not self._lease_live(control, _aware(self._now(), "now_not_timezone_aware")):
            return None
        return {"owner": self._owner, "epoch": held[0]}

    def _publish(
        self,
        operation_id: str,
        stage: str,
        result: StageResult,
        unit: Mapping[str, str] | None,
    ) -> None:
        if not isinstance(stage, str) or _STAGE_KEY.fullmatch(stage) is None:
            _refuse("stage_key_invalid")
        checked = _stage_result(result, "stage_record")
        control, generation, epoch = self._fenced_control(operation_id)
        previous = control["stages"].get(stage)
        attempt = 1 if previous is None else previous["attempt"] + 1
        name = self._record_name(operation_id, stage, epoch, attempt)
        record = {
            "contract_version": STAGE_RECORD_CONTRACT,
            "operation_id": operation_id,
            "stage": stage,
            "attempt": attempt,
            "epoch": epoch,
            "recorded_at": _aware(self._now(), "now_not_timezone_aware").isoformat(),
            "result": checked,
        }
        if unit is not None:
            record["unit"] = dict(unit)
        payload = canonical_bytes(record)
        try:
            self._client.write(name, payload, if_generation_match=0)
        except PreconditionFailed:
            _refuse("stage_record_exists")
        control["stages"][stage] = {
            "reference": name,
            "state": checked["state"],
            "record_digest": sha256(payload).hexdigest(),
            "attempt": attempt,
            "epoch": epoch,
        }
        if unit is not None:
            control["units"][stage] = {
                "reference": name,
                "stage": unit["stage"],
                "permit_digest": unit["permit_digest"],
                "state": checked["state"],
            }
        if checked["state"] == "pending":
            control["dispatch_intent"] = {
                "stage": stage,
                "business_attempt_id": checked["result_reference"],
                "input_digest": checked["input_digest"],
                "epoch": epoch,
            }
        try:
            new_generation = self._write_control(operation_id, control, generation)
        except PreconditionFailed:
            _refuse("stale_owner")
        self._held[operation_id] = (epoch, new_generation)

    def record_stage(self, operation_id: str, stage: str, result: StageResult) -> None:
        self._publish(operation_id, stage, result, None)

    def record_unit(
        self,
        operation_id: str,
        unit_id: str,
        *,
        stage: str,
        permit_digest: str,
        result: StageResult,
    ) -> None:
        if not isinstance(unit_id, str) or "@" in unit_id:
            _refuse("stage_key_invalid")
        if not isinstance(permit_digest, str) or _HEX_64.fullmatch(permit_digest) is None:
            _refuse("permit_digest_invalid")
        self._publish(
            operation_id, unit_id, result, {"stage": stage, "permit_digest": permit_digest}
        )

    def _read_record(self, entry: Mapping[str, object]) -> dict:
        found = self._client.read(entry["reference"])
        if found is None:
            _refuse("stage_record_missing")
        raw, _generation = found
        if sha256(raw).hexdigest() != entry["record_digest"]:
            _refuse("stage_record_conflict")
        record = _unpack(raw, "stage_record_conflict")
        if record.get("contract_version") != STAGE_RECORD_CONTRACT:
            _refuse("stage_record_conflict")
        return record

    def read_stage(self, operation_id: str, stage: str) -> StageResult | None:
        if not isinstance(stage, str) or _STAGE_KEY.fullmatch(stage) is None:
            _refuse("stage_key_invalid")
        found = self._read_control(operation_id)
        if found is None:
            return None
        control, _generation = found
        entry = control["stages"].get(stage)
        if entry is None:
            return None
        return _stage_result(self._read_record(entry)["result"], "stage_record")

    def read_unit_ledger(self, operation_id: str) -> dict[str, dict]:
        """Every unit record of the slot, validated by the kernel's own ledger validator."""
        found = self._read_control(operation_id)
        ledger: dict = {}
        if found is not None:
            control, _generation = found
            for unit_id, entry in control["units"].items():
                record = self._read_record(control["stages"][unit_id])
                ledger[unit_id] = {
                    "stage": entry["stage"],
                    "permit_digest": entry["permit_digest"],
                    "result": record["result"],
                }
        return _validate_unit_ledger(ledger)

    def note_timeline(self, operation_id: str, instants: Mapping[str, object]) -> None:
        """Hold C03 instants for the slot; ``release_claim`` publishes them under the fence."""
        if operation_id not in self._held:
            _refuse("lease_not_held")
        if not isinstance(instants, Mapping) or not set(instants) <= set(TIMELINE_FIELDS):
            _refuse("timeline_invalid")
        noted = self._timeline.setdefault(operation_id, {})
        for field, value in instants.items():
            if value is not None:
                if not isinstance(value, str):
                    _refuse("timeline_invalid")
                try:
                    _aware(datetime.fromisoformat(value), "timeline_invalid")
                except ValueError:
                    _refuse("timeline_invalid")
            noted[field] = value

    def read_timeline(self, operation_id: str) -> dict | None:
        found = self._read_control(operation_id)
        if found is None:
            return None
        return dict(found[0]["timeline"])

    def release_claim(self, operation_id: str) -> None:
        held = self._held.pop(operation_id, None)
        noted = self._timeline.pop(operation_id, {})
        if held is None:
            return
        epoch, _generation = held
        found = self._read_control(operation_id)
        if found is None:
            return
        control, generation = found
        lease = control["lease"]
        if lease is None or lease["owner"] != self._owner or lease["epoch"] != epoch:
            return
        control["lease"] = None
        timeline = control["timeline"]
        for field, value in noted.items():
            if value is not None or field not in timeline:
                timeline[field] = value
        try:
            self._write_control(operation_id, control, generation)
        except PreconditionFailed:
            return
