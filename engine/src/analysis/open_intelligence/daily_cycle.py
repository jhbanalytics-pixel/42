"""Durable daily cycle orchestration kernel.

``execute_daily_cycle`` runs the fixed stage order under an atomic claim,
reuses succeeded stages whose input digest matches, stops on any state that
could hide paid work, and persists a pending intent before every dispatch.
It never manufactures approval: ``DailyAuthority.issue`` consumes the
separately approved recurring grant and returns None when no authority
exists. ``run_validated_daily_cycle`` is the pre-kernel validator that
requires a timezone-aware closed observation-window cutoff and an exact
profile. ``resume_incomplete_units`` is the only path that continues partial
paid work, and it publishes a new attempt version instead of overwriting the
partial record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, time
from hashlib import sha256
from typing import Literal, Protocol, TypedDict

from src.analysis.open_intelligence.brain_contract import canonical_bytes

STAGE_ORDER = ("collect", "capture", "compose", "certify", "release")
STAGE_ORDER_V2 = ("collection", "exposure", "capture", "compose", "certify", "release")
# The step number a v2 child execution runs under: its stage's 1-based position.
STEP_NUMBERS_V2 = {stage: str(index) for index, stage in enumerate(STAGE_ORDER_V2, start=1)}
STAGE_STATES = frozenset({"pending", "unknown", "succeeded", "failed", "partial", "cancelled"})
SUPPORTED_PROFILE_VERSIONS = frozenset({"42_daily_v1"})
_PROFILE_DIGEST_FIELDS = (
    "resource_manifest_digest",
    "source_policy_digest",
    "recurring_grant_digest",
)
_PROFILE_COUNT_FIELDS = ("freshness_target_hours", "max_publish_lag_hours", "max_catchup_cutoffs")
_PROFILE_FIELDS = ("schema_version", *_PROFILE_DIGEST_FIELDS, *_PROFILE_COUNT_FIELDS)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class DailyProfile(TypedDict):
    schema_version: str
    resource_manifest_digest: str
    source_policy_digest: str
    recurring_grant_digest: str
    freshness_target_hours: int
    max_publish_lag_hours: int
    max_catchup_cutoffs: int


class StageResult(TypedDict):
    state: Literal["pending", "unknown", "succeeded", "failed", "partial", "cancelled"]
    input_digest: str
    output_digest: str
    result_reference: str
    retry_safe: bool


class CycleResult(TypedDict):
    state: str
    operation_id: str
    stage: str


class DailyStore(Protocol):
    def claim(self, operation_id: str, cutoff_utc: datetime) -> bool: ...
    def read_stage(self, operation_id: str, stage: str) -> StageResult | None: ...
    def record_stage(self, operation_id: str, stage: str, result: StageResult) -> None: ...
    def release_claim(self, operation_id: str) -> None: ...


class DailyAuthority(Protocol):
    def issue(self, stage: str, manifest: Mapping[str, object]) -> Mapping[str, object] | None: ...


class StageHandler(Protocol):
    def __call__(
        self, *, manifest: Mapping[str, object], authority_receipt: Mapping[str, object]
    ) -> StageResult: ...


def execute_daily_cycle(
    *,
    operation_id: str,
    cutoff_utc: datetime,
    profile: DailyProfile,
    authority: DailyAuthority,
    store: DailyStore,
    stages: Mapping[str, StageHandler],
) -> CycleResult:
    if isinstance(profile, Mapping) and profile.get("schema_version") == "42_daily_v2":
        return execute_daily_cycle_v2(
            operation_id=operation_id,
            cutoff_utc=cutoff_utc,
            profile=profile,
            authority=authority,
            store=store,
            stages=stages,
        )
    if not store.claim(operation_id, cutoff_utc):
        return {"state": "already_claimed", "operation_id": operation_id, "stage": ""}
    predecessor = ""
    try:
        for stage in ("collect", "capture", "compose", "certify", "release"):
            prior = store.read_stage(operation_id, stage)
            if prior:
                if prior["state"] not in {
                    "pending",
                    "unknown",
                    "succeeded",
                    "failed",
                    "partial",
                    "cancelled",
                }:
                    raise ValueError("invalid_stage_state")
                if prior["state"] in {"unknown", "pending", "partial", "cancelled"} or (
                    prior["state"] == "failed" and not prior["retry_safe"]
                ):
                    return {"state": "unavailable", "operation_id": operation_id, "stage": stage}
            manifest = {
                "operation_id": operation_id,
                "stage": stage,
                "cutoff_utc": cutoff_utc.isoformat(),
                "profile": dict(profile),
                "predecessor_digest": predecessor,
            }
            digest = sha256(canonical_bytes(manifest)).hexdigest()
            if prior and prior["input_digest"] != digest:
                raise ValueError("stage_input_conflict")
            if prior and prior["state"] == "succeeded":
                revalidate = getattr(stages[stage], "validate_reuse", None)
                if revalidate is not None:
                    revalidate(manifest=manifest, result=prior)
                predecessor = prior["output_digest"]
                continue
            receipt = authority.issue(stage, manifest)
            if receipt is None:
                state = "release_pending" if stage == "release" else "authority_missing"
                return {"state": state, "operation_id": operation_id, "stage": stage}
            pending = {
                "state": "pending",
                "input_digest": digest,
                "output_digest": "",
                "result_reference": str(receipt["business_attempt_id"]),
                "retry_safe": False,
            }
            store.record_stage(operation_id, stage, pending)
            result = stages[stage](manifest=manifest, authority_receipt=receipt)
            if result["state"] not in {
                "pending",
                "unknown",
                "succeeded",
                "failed",
                "partial",
                "cancelled",
            }:
                raise ValueError("invalid_stage_state")
            if result["input_digest"] != digest:
                raise ValueError("result_input_conflict")
            if result["state"] == "succeeded" and not result["output_digest"]:
                raise ValueError("result_output_missing")
            store.record_stage(operation_id, stage, result)
            if result["state"] != "succeeded":
                return {"state": result["state"], "operation_id": operation_id, "stage": stage}
            predecessor = result["output_digest"]
        return {"state": "succeeded", "operation_id": operation_id, "stage": "release"}
    finally:
        store.release_claim(operation_id)


def execute_daily_cycle_v2(*, operation_id, cutoff_utc, profile, authority, store, stages):
    checked = validate_daily_profile(profile)
    cutoff = _aware(cutoff_utc, "cutoff_utc").astimezone(UTC)
    if cutoff.time() != time.min:
        raise ValueError("slot_cutoff_not_midnight")
    if not isinstance(stages, Mapping) or set(stages) != set(STAGE_ORDER_V2):
        raise ValueError("stage_handlers_inexact")
    if not store.claim_v2(operation_id, "staging", checked["source_estate_id"], cutoff):
        return {"state": "already_claimed", "operation_id": operation_id, "stage": ""}
    predecessor_id, predecessor_digest = None, None
    try:
        for stage in STAGE_ORDER_V2:
            frame = {
                "operation_id": operation_id,
                "stage": stage,
                "cutoff_utc": cutoff.isoformat(),
                "profile": dict(checked),
                "predecessor_result_id": predecessor_id,
                "predecessor_result_digest": predecessor_digest,
            }
            digest = sha256(canonical_bytes(frame)).hexdigest()
            prior = store.read_stage_v2(operation_id, stage, clients=authority.read_clients)
            if prior is not None:
                if prior["input_digest"] != digest:
                    raise ValueError("stage_input_conflict")
                if prior["state"] != "succeeded":
                    return {"state": "unavailable", "operation_id": operation_id, "stage": stage}
                revalidate = getattr(stages[stage], "validate_reuse", None)
                if revalidate is not None:
                    revalidate(manifest=frame, result=prior)
                predecessor_id, predecessor_digest = (
                    prior["result_reference"],
                    prior["output_digest"],
                )
                continue
            prepared = authority.prepare(stage, frame, lease=store.current_lease_v2(operation_id))
            if prepared is None:
                return {
                    "state": "release_pending" if stage == "release" else "authority_missing",
                    "operation_id": operation_id,
                    "stage": stage,
                }
            if (
                not isinstance(prepared, Mapping)
                or not isinstance(prepared.get("intent"), Mapping)
                or not isinstance(prepared.get("operation_context"), Mapping)
            ):
                raise ValueError("stage_preparation_invalid")
            pending, context = prepared["intent"], prepared["operation_context"]
            if (
                pending.get("stage") != stage
                or pending.get("input_digest") != digest
                or pending.get("authorizing_grant_digest") != checked["recurring_grant_digest"]
                or context.get("slot_id") != operation_id
                or context.get("stage") != stage
                or context.get("input_digest") != digest
                or pending.get("operation_context_sha256")
                != sha256(canonical_bytes(context)).hexdigest()
            ):
                raise ValueError("stage_preparation_invalid")
            store.prepare_intent_v2(operation_id, pending)
            derived = authority.issue(stage, prepared)
            if derived is None:
                return {
                    "state": "release_pending" if stage == "release" else "authority_missing",
                    "operation_id": operation_id,
                    "stage": stage,
                }
            store.bind_derivation_v2(operation_id, derived)
            store.mark_dispatch_started_v2(operation_id)
            observed = stages[stage](manifest=frame, authority_receipt=derived)
            if not isinstance(observed, Mapping) or observed.get("state") not in STAGE_STATES:
                raise ValueError("invalid_stage_state")
            if observed["state"] in {"unknown", "pending"}:
                return {"state": observed["state"], "operation_id": operation_id, "stage": stage}
            admitted = store.publish_terminal_v2(
                operation_id, stage, derived["derivation_id"], clients=authority.read_clients
            )
            if admitted["input_digest"] != digest:
                raise ValueError("result_input_conflict")
            if admitted["state"] != "succeeded":
                return {"state": admitted["state"], "operation_id": operation_id, "stage": stage}
            predecessor_id, predecessor_digest = (
                admitted["result_reference"],
                admitted["output_digest"],
            )
        return {"state": "succeeded", "operation_id": operation_id, "stage": "release"}
    finally:
        store.release_claim_v2(operation_id)


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_not_timezone_aware")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"profile_field_invalid:{field}")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"profile_field_invalid:{field}")
    return value


def validate_daily_profile(profile: object) -> DailyProfile:
    """Require an exact profile: every field present, typed and nothing extra."""
    if not isinstance(profile, Mapping):
        raise ValueError("profile_invalid")
    version = profile.get("schema_version")
    fields = set(_PROFILE_FIELDS) | ({"source_estate_id"} if version == "42_daily_v2" else set())
    if set(profile) != fields:
        raise ValueError("profile_fields_inexact")
    if profile["schema_version"] not in SUPPORTED_PROFILE_VERSIONS | {"42_daily_v2"}:
        raise ValueError("profile_schema_unsupported")
    checked: dict = {"schema_version": profile["schema_version"]}
    for field in _PROFILE_DIGEST_FIELDS:
        checked[field] = _digest(profile[field], field)
    for field in _PROFILE_COUNT_FIELDS:
        checked[field] = _count(profile[field], field)
    if version == "42_daily_v2":
        if (
            profile["source_estate_id"] != "intelligence-42-core"
            or not 1 <= checked["max_catchup_cutoffs"] <= 2
        ):
            raise ValueError("profile_field_invalid:source_estate_or_catchup")
        checked["source_estate_id"] = profile["source_estate_id"]
    if checked["freshness_target_hours"] == 0:
        raise ValueError("profile_field_invalid:freshness_target_hours")
    if checked["max_publish_lag_hours"] < checked["freshness_target_hours"]:
        raise ValueError("profile_field_invalid:max_publish_lag_hours")
    return checked  # type: ignore[return-value]


def validate_closed_cutoff(cutoff_utc: object, *, now: object) -> datetime:
    """Require a timezone-aware cutoff whose observation window has already closed."""
    cutoff = _aware(cutoff_utc, "cutoff_utc")
    current = _aware(now, "now")
    if cutoff > current:
        raise ValueError("cutoff_window_open")
    return cutoff


def run_validated_daily_cycle(
    *,
    operation_id: str,
    cutoff_utc: datetime,
    now: datetime,
    profile: DailyProfile,
    authority: DailyAuthority,
    store: DailyStore,
    stages: Mapping[str, StageHandler],
) -> CycleResult:
    """Validate cutoff, profile, operation and handler set before the kernel runs."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id_invalid")
    cutoff = validate_closed_cutoff(cutoff_utc, now=now)
    checked = validate_daily_profile(profile)
    expected_stages = STAGE_ORDER_V2 if checked["schema_version"] == "42_daily_v2" else STAGE_ORDER
    if not isinstance(stages, Mapping) or set(stages) != set(expected_stages):
        raise ValueError("stage_handlers_inexact")
    return execute_daily_cycle(
        operation_id=operation_id,
        cutoff_utc=cutoff,
        profile=checked,
        authority=authority,
        store=store,
        stages=stages,
    )


def _stage_result(value: object, field: str) -> StageResult:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {"state", "input_digest", "output_digest", "result_reference", "retry_safe"}
        or value["state"] not in STAGE_STATES
        or not isinstance(value["input_digest"], str)
        or not isinstance(value["output_digest"], str)
        or not isinstance(value["result_reference"], str)
        or type(value["retry_safe"]) is not bool
    ):
        raise ValueError(f"{field}_invalid")
    return dict(value)  # type: ignore[return-value]


def _validate_unit_ledger(ledger: object) -> dict[str, dict]:
    if not isinstance(ledger, Mapping) or not ledger:
        raise ValueError("unit_ledger_invalid")
    checked: dict[str, dict] = {}
    for unit_id, unit in ledger.items():
        if not isinstance(unit_id, str) or not unit_id:
            raise ValueError("unit_ledger_invalid")
        if (
            not isinstance(unit, Mapping)
            or set(unit) != {"stage", "permit_digest", "result"}
            or unit["stage"] not in STAGE_ORDER
        ):
            raise ValueError("unit_ledger_invalid")
        checked[unit_id] = {
            "stage": unit["stage"],
            "permit_digest": _digest(unit["permit_digest"], "permit_digest"),
            "result": _stage_result(unit["result"], "unit_result"),
        }
    return checked


def resume_incomplete_units(
    *,
    slot_id: str,
    approved_unit_manifest: Mapping[str, object],
    store: DailyStore,
    authority: DailyAuthority,
    stages: Mapping[str, StageHandler],
) -> CycleResult:
    """Continue named uncompleted units of a partial slot under exact resume authority."""
    if not isinstance(slot_id, str) or not slot_id:
        raise ValueError("slot_id_invalid")
    manifest_fields = {
        "slot_id",
        "cutoff_utc",
        "resume_authority_digest",
        "attempt_version",
        "unit_ledger",
        "resume_units",
        "amended_permits",
    }
    if (
        not isinstance(approved_unit_manifest, Mapping)
        or set(approved_unit_manifest) != manifest_fields
    ):
        raise ValueError("resume_manifest_inexact")
    if approved_unit_manifest["slot_id"] != slot_id:
        raise ValueError("resume_slot_mismatch")
    cutoff = _aware(approved_unit_manifest["cutoff_utc"], "cutoff_utc")
    resume_digest = _digest(
        approved_unit_manifest["resume_authority_digest"], "resume_authority_digest"
    )
    attempt_version = approved_unit_manifest["attempt_version"]
    if (
        isinstance(attempt_version, bool)
        or not isinstance(attempt_version, int)
        or attempt_version < 2
    ):
        raise ValueError("attempt_version_invalid")
    ledger = _validate_unit_ledger(approved_unit_manifest["unit_ledger"])
    resume_units = approved_unit_manifest["resume_units"]
    if (
        not isinstance(resume_units, list | tuple)
        or not resume_units
        or len(resume_units) != len(set(resume_units))
    ):
        raise ValueError("resume_units_invalid")
    amended = approved_unit_manifest["amended_permits"]
    if not isinstance(amended, Mapping):
        raise ValueError("amended_permits_invalid")
    for unit_id, permit in amended.items():
        if unit_id not in ledger:
            raise ValueError("unknown_unit")
        _digest(permit, "amended_permit")
    for unit_id in resume_units:
        if unit_id not in ledger:
            raise ValueError("unknown_unit")
        state = ledger[unit_id]["result"]["state"]
        if state == "unknown":
            raise ValueError("unit_state_unknown")
        if state == "succeeded":
            raise ValueError("unit_already_succeeded")
        if ledger[unit_id]["stage"] not in stages:
            raise ValueError("stage_handler_missing")
    if not store.claim(slot_id, cutoff):
        return {"state": "already_claimed", "operation_id": slot_id, "stage": ""}
    try:
        for unit_id, unit in ledger.items():
            stored = store.read_stage(slot_id, unit_id)
            if stored is None:
                raise ValueError("unit_ledger_incomplete")
            if dict(stored) != unit["result"]:
                raise ValueError("unit_ledger_conflict")
        for unit_id in resume_units:
            if store.read_stage(slot_id, f"{unit_id}@{attempt_version}") is not None:
                raise ValueError("attempt_version_exists")
        receipt = authority.issue("resume", dict(approved_unit_manifest))
        if receipt is None:
            return {"state": "authority_missing", "operation_id": slot_id, "stage": "resume"}
        if receipt.get("resume_authority_digest") != resume_digest:
            raise ValueError("resume_authority_mismatch")
        for unit_id in resume_units:
            unit = ledger[unit_id]
            versioned = f"{unit_id}@{attempt_version}"
            manifest = {
                "slot_id": slot_id,
                "unit_id": unit_id,
                "stage": unit["stage"],
                "attempt_version": attempt_version,
                "cutoff_utc": cutoff.isoformat(),
                "resume_authority_digest": resume_digest,
                "permit_digest": amended.get(unit_id, unit["permit_digest"]),
                "original_result": unit["result"],
            }
            digest = sha256(canonical_bytes(manifest)).hexdigest()
            unit_receipt = authority.issue(unit_id, manifest)
            if unit_receipt is None:
                return {"state": "authority_missing", "operation_id": slot_id, "stage": versioned}
            pending = {
                "state": "pending",
                "input_digest": digest,
                "output_digest": "",
                "result_reference": str(unit_receipt["business_attempt_id"]),
                "retry_safe": False,
            }
            store.record_stage(slot_id, versioned, pending)
            result = stages[unit["stage"]](manifest=manifest, authority_receipt=unit_receipt)
            if result["state"] not in STAGE_STATES:
                raise ValueError("invalid_stage_state")
            if result["input_digest"] != digest:
                raise ValueError("result_input_conflict")
            if result["state"] == "succeeded" and not result["output_digest"]:
                raise ValueError("result_output_missing")
            store.record_stage(slot_id, versioned, result)
            if result["state"] != "succeeded":
                return {"state": result["state"], "operation_id": slot_id, "stage": versioned}
        return {"state": "succeeded", "operation_id": slot_id, "stage": "resume"}
    finally:
        store.release_claim(slot_id)
