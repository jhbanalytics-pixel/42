"""DailyAuthority: exact per-stage execution authority under the recurring grant.

``DailyAuthority.issue(stage, manifest)`` is the kernel's ``DailyAuthority``
protocol. It never manufactures approval. For every stage it checks the grant
(``recurring_grant.check_grant``), confirms the caller still holds the slot
lease (the fencing epoch travels into the authority record), then consumes
the grant through the engine authority adapter the managed runtime binds:
``reserve`` is ``_load_execution_authority`` and ``consume`` is
``_consume_execution_authority`` in ``execution_approval``. Issuance is
idempotent by the stable business attempt (slot plus stage attempt) and the
canonical manifest digest: the durable authority record is created once, and
a second issue for the same pair returns the same authority and reservation
without a second paid consumption. The business attempt id is preassigned;
no execution name is ever invented. A release stage returns None
(release_pending) while no human release authority or approved recurring
delegation covers it.

The receipt carries, under ``execution``, the issued execution authority beside
its durable reference: the authority object the reservation path answered and
the consumption it returned, the exact objects the capture runner body must
present, kept in process per business attempt so a second issue returns the
same objects; the plain consumption and the record's name, which the durable
record keeps; the stage, the grant operation, the execution mode and the
generation the authority carries. A record issued by an earlier process carries
the durable reference alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import fields
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import STAGE_ORDER, validate_daily_profile
from src.analysis.open_intelligence.recurring_grant import (
    RELEASE_MANIFEST_CONTRACT,
    STAGE_OPERATIONS,
    V1_REGISTRY_OPERATIONS,
    check_grant,
    grant_digest,
    validate_recurring_grant,
)

AUTHORITY_CONTRACT = "42_daily_authority_v1"
RELEASE_OPERATION = STAGE_OPERATIONS["release"]
AUTHORITY_PREFIX = "42/daily/authority"
EXECUTION_MODE = "new_consume"
EXECUTION_FIELDS = frozenset(
    {
        "authority",
        "consumption",
        "reservation",
        "authority_reference",
        "stage",
        "operation",
        "mode",
        "generation",
    }
)
# The issued authority and its consumption per business attempt, kept beside the plain
# consumption the durable record holds; process wide, like the engine's own registry.
_ISSUED_AUTHORITIES: dict[str, tuple[object, object, object]] = {}
_KERNEL_MANIFEST_FIELDS = frozenset(
    {"operation_id", "stage", "cutoff_utc", "profile", "predecessor_digest"}
)
_RESUME_MANIFEST_FIELDS = frozenset(
    {
        "slot_id",
        "cutoff_utc",
        "resume_authority_digest",
        "attempt_version",
        "unit_ledger",
        "resume_units",
        "amended_permits",
    }
)
_UNIT_MANIFEST_FIELDS = frozenset(
    {
        "slot_id",
        "unit_id",
        "stage",
        "attempt_version",
        "cutoff_utc",
        "resume_authority_digest",
        "permit_digest",
        "original_result",
    }
)
_ATTEMPT_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class AuthorityRefusal(ValueError):
    """Refusals raised by the authority itself; grant refusals keep their own family."""


class AuthorityRecordExists(Exception):
    """Raised by a ledger when a create-only authority record already exists."""


class AuthorityLedger(Protocol):
    def read(self, attempt_id: str) -> Mapping[str, object] | None: ...
    def create(self, attempt_id: str, record: Mapping[str, object]) -> str: ...


class ReservationPath(Protocol):
    def reserve(self, invocation: Mapping[str, object], run_manifest: Mapping[str, object]): ...
    def consume(self, authority: object) -> object: ...


def _refuse(code: str) -> None:
    raise AuthorityRefusal(code)


def _current_lease(value: object) -> dict[str, object]:
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("owner"), str)
        or not value["owner"]
        or isinstance(value.get("epoch"), bool)
        or not isinstance(value.get("epoch"), int)
        or value["epoch"] < 1
    ):
        _refuse("authority_lease_lost")
    return {"owner": value["owner"], "epoch": value["epoch"]}


def business_attempt_id(operation_id: str, attempt_key: str) -> str:
    """Stable business attempt: the slot plus the stage attempt key, nothing time bound."""
    if not isinstance(operation_id, str) or not operation_id:
        _refuse("authority_manifest_invalid")
    payload = {"operation_id": operation_id, "attempt": attempt_key}
    return "bat_" + sha256(canonical_bytes(payload)).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _cutoff(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            _refuse("authority_manifest_invalid")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse("authority_manifest_invalid")
    return value.astimezone(UTC)


class ObjectAuthorityLedger:
    """Create-only authority records over the evidence bucket object client."""

    def __init__(self, client, *, prefix: str = AUTHORITY_PREFIX):
        self._client = client
        self._prefix = prefix

    def _name(self, attempt_id: str) -> str:
        return f"{self._prefix}/{attempt_id}.json"

    def read(self, attempt_id: str) -> Mapping[str, object] | None:
        import json

        found = self._client.read(self._name(attempt_id))
        if found is None:
            return None
        raw, _generation = found
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict) or record.get("contract_version") != AUTHORITY_CONTRACT:
            _refuse("authority_record_invalid")
        return record

    def create(self, attempt_id: str, record: Mapping[str, object]) -> str:
        from src.analysis.open_intelligence.daily_store import PreconditionFailed

        name = self._name(attempt_id)
        try:
            self._client.write(name, canonical_bytes(record), if_generation_match=0)
        except PreconditionFailed as error:
            raise AuthorityRecordExists(attempt_id) from error
        return name


class DailyAuthority:
    def __init__(
        self,
        *,
        grant: Mapping[str, object],
        profile: Mapping[str, object],
        principal: str,
        image_digest: str,
        resource_manifest_digest: str,
        reservation: ReservationPath,
        ledger: AuthorityLedger,
        fence: Callable[[], Mapping[str, object] | None],
        now: Callable[[], datetime],
        release_delegation: Callable[[], Mapping[str, object] | None] | None = None,
        resume_authority: Callable[[], Mapping[str, object] | None] | None = None,
    ):
        self._grant = validate_recurring_grant(grant)
        self._grant_digest = grant_digest(self._grant)
        self._profile = validate_daily_profile(profile)
        if self._profile["recurring_grant_digest"] != self._grant_digest:
            _refuse("authority_profile_grant_mismatch")
        if self._profile["schema_version"] != self._grant["schema_version"]:
            _refuse("authority_profile_grant_mismatch")
        self._principal = principal
        self._image_digest = image_digest
        self._resource_manifest_digest = resource_manifest_digest
        self._reservation = reservation
        self._ledger = ledger
        self._fence = fence
        self._now = now
        self._release_delegation = release_delegation
        self._resume_authority = resume_authority

    def _classify(self, stage: str, manifest: Mapping[str, object]) -> dict:
        if not isinstance(stage, str) or not isinstance(manifest, Mapping):
            _refuse("authority_manifest_invalid")
        if stage in STAGE_ORDER:
            if set(manifest) != _KERNEL_MANIFEST_FIELDS or manifest["stage"] != stage:
                _refuse("authority_manifest_invalid")
            if dict(manifest["profile"]) != dict(self._profile):
                _refuse("authority_manifest_invalid")
            return {
                "operation_id": manifest["operation_id"],
                "attempt_key": stage,
                "operation": STAGE_OPERATIONS[stage],
                "cutoff_utc": _cutoff(manifest["cutoff_utc"]),
            }
        if stage == "resume":
            if set(manifest) != _RESUME_MANIFEST_FIELDS:
                _refuse("authority_manifest_invalid")
            return {
                "operation_id": manifest["slot_id"],
                "attempt_key": "resume",
                "operation": "resume",
                "cutoff_utc": _cutoff(manifest["cutoff_utc"]),
            }
        if (
            _ATTEMPT_KEY.fullmatch(stage) is None
            or set(manifest) != _UNIT_MANIFEST_FIELDS
            or manifest["unit_id"] != stage
            or manifest["stage"] not in STAGE_ORDER
            or isinstance(manifest["attempt_version"], bool)
            or not isinstance(manifest["attempt_version"], int)
        ):
            _refuse("authority_manifest_invalid")
        return {
            "operation_id": manifest["slot_id"],
            "attempt_key": f"{stage}@{manifest['attempt_version']}",
            "operation": STAGE_OPERATIONS[manifest["stage"]],
            "cutoff_utc": _cutoff(manifest["cutoff_utc"]),
        }

    def _release_delegated(self) -> bool:
        if self._release_delegation is None:
            return False
        manifest = self._release_delegation()
        if manifest is None:
            return False
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("contract_version") != RELEASE_MANIFEST_CONTRACT
            or manifest.get("delegation") not in {"none", "recurring"}
        ):
            _refuse("authority_release_manifest_invalid")
        digest = sha256(canonical_bytes(manifest)).hexdigest()
        if digest != self._grant["approved_release_manifest_digest"]:
            _refuse("authority_release_manifest_mismatch")
        return manifest["delegation"] == "recurring"

    def _receipt(self, record: Mapping[str, object], reference: str) -> dict:
        receipt = {
            "business_attempt_id": record["business_attempt_id"],
            "authority_reference": reference,
            "manifest_digest": record["manifest_digest"],
            "operation": record["operation"],
            "stage": record["stage"],
            "grant_id": record["grant_id"],
            "grant_digest": record["grant_digest"],
            "lease_epoch": record["lease"]["epoch"],
            "reservation": record["reservation"],
        }
        if "resume_authority_digest" in record:
            receipt["resume_authority_digest"] = record["resume_authority_digest"]
        receipt["execution"] = self._execution(record)
        return receipt

    @staticmethod
    def _execution(record: Mapping[str, object]) -> dict:
        """The issued execution authority of the record beside its durable reference."""
        issued = _ISSUED_AUTHORITIES.get(record["business_attempt_id"])
        authority, consumption = (None, None)
        if issued is not None and issued[2] == record["reservation"]:
            authority, consumption, _plain_consumption = issued
        return {
            "authority": authority,
            "consumption": consumption,
            "reservation": record["reservation"],
            "authority_reference": record["authority_reference"],
            "stage": record["stage"],
            "operation": record["operation"],
            "mode": EXECUTION_MODE,
            "generation": getattr(authority, "generation", None),
        }

    def issue(self, stage: str, manifest: Mapping[str, object]) -> Mapping[str, object] | None:
        shape = self._classify(stage, manifest)
        attempt = business_attempt_id(shape["operation_id"], shape["attempt_key"])
        manifest_digest = sha256(canonical_bytes(manifest)).hexdigest()
        existing = self._ledger.read(attempt)
        if existing is not None:
            if (
                existing.get("contract_version") != AUTHORITY_CONTRACT
                or existing.get("business_attempt_id") != attempt
            ):
                _refuse("authority_record_invalid")
            if existing.get("manifest_digest") != manifest_digest:
                _refuse("authority_manifest_conflict")
            if existing.get("lease") != _current_lease(self._fence()):
                _refuse("authority_takeover_reconciliation_required")
            return self._receipt(existing, existing["authority_reference"])
        resume_record = None
        if shape["operation"] == "resume":
            if self._resume_authority is None:
                return None
            resume_record = self._resume_authority()
            if resume_record is None:
                return None
            if (
                not isinstance(resume_record, Mapping)
                or not isinstance(resume_record.get("resume_authority_digest"), str)
                or _HEX_64.fullmatch(resume_record["resume_authority_digest"]) is None
            ):
                _refuse("authority_resume_record_invalid")
        elif shape["operation"] == RELEASE_OPERATION and not self._release_delegated():
            return None
        else:
            check_grant(
                self._grant,
                now=self._now(),
                operation=shape["operation"],
                principal=self._principal,
                manifest_digest=self._resource_manifest_digest,
                source_policy_digest=self._profile["source_policy_digest"],
                image_digest=self._image_digest,
                cutoff_utc=shape["cutoff_utc"],
            )
        lease = _current_lease(self._fence())
        reservation = None
        issued = None
        if resume_record is None:
            # The grant admitted the C03 name above; the reservation path resolves the
            # registry name the origin registry binds for the same stage.
            run_manifest = {
                "operation": V1_REGISTRY_OPERATIONS[shape["operation"]],
                "manifest_sha256": manifest_digest,
                "business_attempt_id": attempt,
                "stage": stage,
                "grant_id": self._grant["grant_id"],
                "grant_digest": self._grant_digest,
                "lease_epoch": lease["epoch"],
            }
            invocation = {
                "principal": self._principal,
                "image_digest": self._image_digest,
                "resource_manifest_digest": self._resource_manifest_digest,
                "business_attempt_id": attempt,
            }
            try:
                authority = self._reservation.reserve(invocation, run_manifest)
                consumed = self._reservation.consume(authority)
            except AuthorityRefusal:
                raise
            except Exception as error:
                raise AuthorityRefusal("authority_unresolved") from error
            from .execution_approval import ExecutionConsumptionV2

            if isinstance(consumed, ExecutionConsumptionV2):
                reservation = _plain(
                    {field.name: getattr(consumed, field.name) for field in fields(consumed)}
                )
            elif isinstance(consumed, Mapping):
                reservation = _plain(consumed)
            else:
                _refuse("authority_unresolved")
            if "execution_name" in reservation and not reservation["execution_name"]:
                _refuse("authority_unresolved")
            issued = (authority, consumed, reservation)
        record = {
            "contract_version": AUTHORITY_CONTRACT,
            "business_attempt_id": attempt,
            "authority_reference": f"{AUTHORITY_PREFIX}/{attempt}.json",
            "operation_id": shape["operation_id"],
            "attempt_key": shape["attempt_key"],
            "manifest_digest": manifest_digest,
            "operation": shape["operation"],
            "stage": stage,
            "grant_id": self._grant["grant_id"],
            "grant_digest": self._grant_digest,
            "principal": self._principal,
            "image_digest": self._image_digest,
            "lease": {"owner": lease["owner"], "epoch": lease["epoch"]},
            "issued_at": self._now().astimezone(UTC).isoformat(),
            "reservation": reservation,
        }
        if resume_record is not None:
            record["resume_authority_digest"] = resume_record["resume_authority_digest"]
        try:
            reference = self._ledger.create(attempt, record)
        except AuthorityRecordExists as error:
            raise AuthorityRefusal("authority_unresolved") from error
        if issued is not None:
            _ISSUED_AUTHORITIES[attempt] = issued
        return self._receipt(record, reference)
