"""Canonical validators for daily execution authority records."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from src.analysis.open_intelligence.brain_contract import canonical_bytes

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DERIVATION_ID = re.compile(r"^exd_[0-9a-f]{64}$")
CONSUMPTION_ID = re.compile(r"^exc_[0-9a-f]{64}$")
RESULT_ID = re.compile(r"^exr_[0-9a-f]{64}$")
OPERATIONS = {
    "daily_source_collection": "collection",
    "daily_collection_exposure_issue": "exposure",
    "daily_source_snapshot_capture": "capture",
    "daily_composition_apply": "compose",
    "daily_quality_proof_issue": "certify",
    "daily_staging_release": "release",
}
CONTEXT_FIELDS = {
    "attempt_version",
    "authorizing_approval_id",
    "authorizing_grant_digest",
    "business_attempt_id",
    "child_image_uri",
    "child_job_policy_digest",
    "child_job_resource",
    "child_service_identity",
    "contract_version",
    "cutoff_utc",
    "execution_observation_prefix",
    "gcs_control_object",
    "input_digest",
    "intent_id",
    "lease_epoch",
    "lease_owner",
    "mode",
    "operation",
    "operation_artifact_set_sha256",
    "parent_execution_name",
    "parent_job_resource",
    "parent_principal",
    "predecessor_result_digest",
    "predecessor_result_id",
    "profile_digest",
    "slot_id",
    "source_estate_id",
    "stage",
}
OBSERVATION_FIELDS = {
    "authorizing_grant_digest",
    "child_image_uri",
    "child_job_policy_digest",
    "child_job_resource",
    "child_service_identity",
    "contract_version",
    "derivation_id",
    "execution_created_at",
    "execution_name",
    "execution_started_at",
    "observed_at",
    "observer_principal",
}
ARTIFACT_SET_FIELDS = {"artifacts", "contract_version"}
ARTIFACT_FIELDS = {"data", "encoding", "name"}
RECONCILIATION_FIELDS = {
    "contract_version",
    "derivation_id",
    "dispatch_attempted",
    "dispatch_observation_reference",
    "execution_created",
    "observed_at",
    "observer_principal",
    "provider_operation_name",
    "provider_terminal_state",
    "reason_code",
}
MAX_ARTIFACT_BYTES = 524_288
MAX_ARTIFACT_SET_BYTES = 4_194_304


class DailyContractError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(condition: bool, code: str) -> None:
    if condition:
        raise DailyContractError(code)


def _digest(value: object, code: str) -> str:
    _refuse(not isinstance(value, str) or HEX64.fullmatch(value) is None, code)
    return value


def _instant(value: object, code: str) -> datetime:
    _refuse(not isinstance(value, str), code)
    fraction = re.search(r"\.(\d+)(?:Z|[+-])", value)
    _refuse(fraction is not None and any(digit != "0" for digit in fraction.group(1)[6:]), code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DailyContractError(code) from error
    _refuse(parsed.tzinfo is None or parsed.utcoffset() is None, code)
    return parsed.astimezone(UTC)


def canonical_json_object(value: object, code: str) -> tuple[dict[str, object], bytes]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
        try:
            parsed = json.loads(value)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DailyContractError(code) from error
    elif isinstance(value, bytes):
        raw = value
        try:
            parsed = json.loads(value.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DailyContractError(code) from error
    else:
        parsed = value
        raw = canonical_bytes(value)
    _refuse(not isinstance(parsed, dict) or canonical_bytes(parsed) != raw, code)
    return parsed, raw


def validate_operation_context(value: object) -> dict[str, object]:
    context, _ = canonical_json_object(value, "daily_context_noncanonical")
    _refuse(set(context) != CONTEXT_FIELDS, "daily_context_fields_invalid")
    operation = context["operation"]
    _refuse(operation not in OPERATIONS, "daily_context_operation_invalid")
    _refuse(
        context["contract_version"] != "daily_operation_context_v1", "daily_context_version_invalid"
    )
    _refuse(context["stage"] != OPERATIONS[operation], "daily_context_stage_invalid")
    _refuse(context["source_estate_id"] != "intelligence-42-core", "daily_context_estate_invalid")
    _refuse(
        context["execution_observation_prefix"] != "42/daily/execution-observations/",
        "daily_context_observation_prefix_invalid",
    )
    expected_mode = (
        {"initial", "recover"} if operation == "daily_source_snapshot_capture" else {"run"}
    )
    _refuse(context["mode"] not in expected_mode, "daily_context_mode_invalid")
    _refuse(
        type(context["attempt_version"]) is not int or context["attempt_version"] <= 0,
        "daily_context_attempt_invalid",
    )
    _refuse(
        type(context["lease_epoch"]) is not int or context["lease_epoch"] <= 0,
        "daily_context_lease_invalid",
    )
    cutoff = _instant(context["cutoff_utc"], "daily_context_cutoff_invalid")
    _refuse(
        cutoff.hour != 0 or cutoff.minute != 0 or cutoff.second != 0 or cutoff.microsecond != 0,
        "daily_context_cutoff_invalid",
    )
    _refuse(context["cutoff_utc"] != cutoff.isoformat(), "daily_context_cutoff_noncanonical")
    for name in (
        "authorizing_grant_digest",
        "child_job_policy_digest",
        "input_digest",
        "operation_artifact_set_sha256",
        "profile_digest",
        "slot_id",
    ):
        _digest(context[name], "daily_context_digest_invalid")
    predecessor = context["predecessor_result_id"], context["predecessor_result_digest"]
    if operation == "daily_source_collection":
        _refuse(predecessor != (None, None), "daily_context_predecessor_invalid")
    else:
        _refuse(
            not isinstance(predecessor[0], str) or RESULT_ID.fullmatch(predecessor[0]) is None,
            "daily_context_predecessor_invalid",
        )
        _digest(predecessor[1], "daily_context_predecessor_invalid")
    return context


def validate_artifact_set(value: object) -> tuple[dict[str, bytes], bytes]:
    envelope, raw = canonical_json_object(value, "daily_artifact_set_noncanonical")
    _refuse(
        set(envelope) != ARTIFACT_SET_FIELDS
        or envelope["contract_version"] != "daily_operation_artifact_set_v1",
        "daily_artifact_set_invalid",
    )
    artifacts = envelope["artifacts"]
    _refuse(not isinstance(artifacts, list) or not artifacts, "daily_artifact_set_invalid")
    decoded: dict[str, bytes] = {}
    names: list[str] = []
    total = 0
    for item in artifacts:
        _refuse(
            not isinstance(item, dict) or set(item) != ARTIFACT_FIELDS, "daily_artifact_invalid"
        )
        name = item["name"]
        _refuse(
            not isinstance(name, str) or not name or name == "operation_context",
            "daily_artifact_name_invalid",
        )
        _refuse(
            item["encoding"] != "base64" or not isinstance(item["data"], str),
            "daily_artifact_encoding_invalid",
        )
        try:
            value_bytes = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError) as error:
            raise DailyContractError("daily_artifact_encoding_invalid") from error
        _refuse(
            base64.b64encode(value_bytes).decode("ascii") != item["data"],
            "daily_artifact_encoding_invalid",
        )
        _refuse(len(value_bytes) > MAX_ARTIFACT_BYTES, "daily_artifact_size_exceeded")
        _refuse(name in decoded, "daily_artifact_duplicate")
        decoded[name] = value_bytes
        names.append(name)
        total += len(value_bytes)
    _refuse(names != sorted(names), "daily_artifact_order_invalid")
    _refuse(total > MAX_ARTIFACT_SET_BYTES, "daily_artifact_set_size_exceeded")
    return decoded, raw


def validate_execution_observation(value: object) -> tuple[dict[str, object], bytes]:
    observation, raw = canonical_json_object(value, "daily_observation_noncanonical")
    _refuse(set(observation) != OBSERVATION_FIELDS, "daily_observation_fields_invalid")
    _refuse(
        observation["contract_version"] != "daily_native_execution_observation_v1",
        "daily_observation_version_invalid",
    )
    _refuse(
        not isinstance(observation["derivation_id"], str)
        or DERIVATION_ID.fullmatch(observation["derivation_id"]) is None,
        "daily_observation_derivation_invalid",
    )
    _digest(observation["authorizing_grant_digest"], "daily_observation_grant_invalid")
    created = _instant(observation["execution_created_at"], "daily_observation_time_invalid")
    started = _instant(observation["execution_started_at"], "daily_observation_time_invalid")
    observed = _instant(observation["observed_at"], "daily_observation_time_invalid")
    _refuse(not created <= started <= observed, "daily_observation_time_invalid")
    for name in OBSERVATION_FIELDS - {
        "contract_version",
        "derivation_id",
        "authorizing_grant_digest",
        "execution_created_at",
        "execution_started_at",
        "observed_at",
    }:
        _refuse(
            not isinstance(observation[name], str) or not observation[name],
            "daily_observation_value_invalid",
        )
    _refuse(
        observation["observer_principal"] != observation["child_service_identity"],
        "daily_observation_identity_invalid",
    )
    return observation, raw


_TIME_NOT_PROVIDED = object()


def validate_observation_time_bounds(observation, *, read_at, stored_at=_TIME_NOT_PROVIDED):
    code = "daily_observation_time_invalid"
    _refuse(not isinstance(read_at, datetime), code)
    observed = _instant(observation["observed_at"], code)
    actual_read = _instant(read_at.isoformat(), code)
    _refuse(observed > actual_read, code)
    if stored_at is not _TIME_NOT_PROVIDED:
        created = _instant(
            stored_at.isoformat() if isinstance(stored_at, datetime) else stored_at, code
        )
        _refuse(not observed <= created <= actual_read, code)


def validate_result_metering(value, *, terminal_state, effect_state, spend_state):
    metering, _raw = canonical_json_object(value, "daily_result_metering_invalid")
    integer_fields = (
        "query_count",
        "total_bytes_billed",
        "storage_write_count",
        "storage_write_bytes",
        "model_calls",
    )
    _refuse(
        set(metering) != {*integer_fields, "vendor_credits", "complete"},
        "daily_result_metering_invalid",
    )
    _refuse(type(metering["complete"]) is not bool, "daily_result_metering_invalid")
    for field in integer_fields:
        measured = metering[field]
        _refuse(
            measured is not None and (type(measured) is not int or measured < 0),
            "daily_result_metering_invalid",
        )
    credits = metering["vendor_credits"]
    _refuse(
        credits is not None
        and (
            not isinstance(credits, str)
            or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", credits) is None
        ),
        "daily_result_metering_invalid",
    )
    _refuse(
        terminal_state not in {"succeeded", "failed"}
        or effect_state not in {"effects_recorded", "no_effect", "unknown"}
        or spend_state not in {"measured", "no_spend", "unknown"},
        "daily_result_state_invalid",
    )
    _refuse(
        metering["complete"]
        and any(metering[key] is None for key in (*integer_fields, "vendor_credits")),
        "daily_result_metering_invalid",
    )
    _refuse(
        not metering["complete"] and spend_state != "unknown", "daily_result_metering_incomplete"
    )
    _refuse(
        terminal_state == "succeeded" and effect_state != "effects_recorded",
        "daily_result_state_invalid",
    )
    _refuse(
        spend_state == "no_spend"
        and (
            not metering["complete"]
            or effect_state != "no_effect"
            or credits != "0"
            or any(metering[key] != 0 for key in integer_fields)
        ),
        "daily_result_state_invalid",
    )
    return metering


def validate_reconciliation(value: object) -> tuple[dict[str, object], bytes]:
    record, raw = canonical_json_object(value, "daily_reconciliation_noncanonical")
    _refuse(set(record) != RECONCILIATION_FIELDS, "daily_reconciliation_invalid")
    _refuse(
        record["contract_version"] != "daily_dispatch_reconciliation_v1"
        or not isinstance(record["derivation_id"], str)
        or DERIVATION_ID.fullmatch(record["derivation_id"]) is None
        or type(record["dispatch_attempted"]) is not bool
        or type(record["execution_created"]) is not bool
        or not isinstance(record["observer_principal"], str)
        or not record["observer_principal"],
        "daily_reconciliation_invalid",
    )
    _instant(record["observed_at"], "daily_reconciliation_invalid")
    reason = record["reason_code"]
    if reason == "dispatch_not_attempted":
        _refuse(
            record["dispatch_attempted"]
            or record["execution_created"]
            or record["provider_operation_name"] is not None
            or record["provider_terminal_state"] is not None
            or record["dispatch_observation_reference"] is not None,
            "daily_reconciliation_invalid",
        )
    elif reason == "provider_terminal_no_execution":
        _refuse(
            not record["dispatch_attempted"]
            or record["execution_created"]
            or not isinstance(record["provider_operation_name"], str)
            or not record["provider_operation_name"]
            or record["provider_terminal_state"] != "done_no_execution"
            or not isinstance(record["dispatch_observation_reference"], str)
            or not record["dispatch_observation_reference"],
            "daily_reconciliation_invalid",
        )
    else:
        raise DailyContractError("daily_reconciliation_invalid")
    return record, raw


_CAPTURE_ARTIFACTS = frozenset(
    {
        "build_provenance",
        "capture_contract",
        "capture_plan",
        "cost_policy",
        "daily_profile",
        "recovery_context",
        "source_metadata",
        "storage_policy",
    }
)
_BRIDGE_ARTIFACTS = frozenset(
    {"bridge_policy", "collection_receipt_set", "history_completion_set", "temporal_rules"}
)
_CAPTURE_OPERATION = "daily_source_snapshot_capture"
_CAPTURE_PLAN_V2 = "open_intelligence_protected_capture_plan_v2"
_CAPTURE_PLAN_V3 = "open_intelligence_protected_capture_plan_v3"


def _validate_bridge_capture_plan(
    plan: Mapping[str, object],
    storage: Mapping[str, object],
    artifacts: Mapping[str, bytes],
    *,
    closed_date: str,
    now: object,
) -> None:
    """Regenerate a v3 plan from the bound artifacts; its own fields prove nothing."""
    from src.analysis.open_intelligence.source_estate_bridge import MARKETS
    from src.analysis.open_intelligence.source_estate_bridge_plan import validate_bridge_plan
    from src.contracts.open_intelligence import resolve_client_scope

    _refuse(
        not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None,
        "daily_capture_clock_invalid",
    )
    _refuse(
        storage.get("contract_version") != "open_intelligence_source_capture_storage_v2"
        or not isinstance(storage.get("grant"), Mapping)
        or not isinstance(plan.get("snapshot_plan"), Mapping)
        or plan["snapshot_plan"].get("source_estate_digest")
        != storage["grant"].get("source_estate_digest"),
        "daily_capture_storage_invalid",
    )
    _refuse(plan.get("cutoff_date") != closed_date, "daily_capture_plan_invalid")
    try:
        # The scope is the configured client scope the plan names, over every bridge market.
        scope = resolve_client_scope(
            run_id=_CAPTURE_OPERATION,
            client_scope_id=plan.get("client_scope_id"),
            market_scope=MARKETS,
        )
        bridge = {
            name: canonical_json_object(artifacts[name], "daily_capture_plan_invalid")[0]
            for name in sorted(_BRIDGE_ARTIFACTS)
        }
        metadata, _ = canonical_json_object(
            artifacts["source_metadata"], "daily_capture_plan_invalid"
        )
        validate_bridge_plan(
            artifacts["capture_plan"],
            profile=json.loads(canonical_bytes(plan["snapshot_plan"])),
            source_metadata=metadata,
            storage_policy=storage,
            cutoff_date=closed_date,
            client_scope_id=scope.client_scope_id,
            now=now,
            artifacts=bridge,
        )
    except (TypeError, ValueError) as error:
        raise DailyContractError("daily_capture_plan_invalid") from error


def validate_capture_semantics(
    context: Mapping[str, object], artifacts: Mapping[str, bytes], *, now: object = None
) -> None:
    """Check a capture operation's plan against its context and bound artifacts.

    A v2 plan keeps its field checks. A v3 bridge plan also needs its four bridge artifacts
    and the caller's clock, and is regenerated from them byte for byte.
    """
    _refuse(
        set(artifacts) - _BRIDGE_ARTIFACTS != _CAPTURE_ARTIFACTS,
        "daily_capture_artifact_set_invalid",
    )
    plan, _ = canonical_json_object(artifacts["capture_plan"], "daily_capture_plan_invalid")
    version = plan.get("contract_version")
    required = _CAPTURE_ARTIFACTS | (_BRIDGE_ARTIFACTS if version == _CAPTURE_PLAN_V3 else set())
    _refuse(set(artifacts) != required, "daily_capture_artifact_set_invalid")
    storage, _ = canonical_json_object(artifacts["storage_policy"], "daily_capture_storage_invalid")
    cutoff = _instant(context["cutoff_utc"], "daily_capture_cutoff_invalid")
    closed_date = (cutoff.date() - timedelta(days=1)).isoformat()
    if version == _CAPTURE_PLAN_V3:
        _validate_bridge_capture_plan(plan, storage, artifacts, closed_date=closed_date, now=now)
    else:
        _refuse(
            version != _CAPTURE_PLAN_V2
            or plan.get("cutoff_date") != closed_date
            or not isinstance(plan.get("snapshot_plan"), Mapping)
            or plan["snapshot_plan"].get("profile_version") != "42_staging_source_v2"
            or plan["snapshot_plan"].get("projection_version") != "native_id_bound_v1"
            or plan["snapshot_plan"].get("source_dataset") != "intelligence_42_sources_staging"
            or _instant(
                plan["snapshot_plan"].get("observation_window_end"),
                "daily_capture_plan_invalid",
            )
            != cutoff,
            "daily_capture_plan_invalid",
        )
        _refuse(
            storage.get("contract_version") != "open_intelligence_source_capture_storage_v2"
            or not isinstance(storage.get("grant"), Mapping)
            or plan["snapshot_plan"].get("source_estate_digest")
            != storage["grant"].get("source_estate_digest"),
            "daily_capture_storage_invalid",
        )
    recovery_raw = artifacts["recovery_context"]
    if context["mode"] == "initial":
        _refuse(recovery_raw != b"null", "daily_capture_recovery_invalid")
    else:
        recovery, _ = canonical_json_object(recovery_raw, "daily_capture_recovery_invalid")
        _refuse(
            recovery.get("contract_version") != "open_intelligence_daily_source_capture_recovery_v1"
            or recovery.get("initial_operation") != "daily_source_snapshot_capture"
            or recovery.get("initial_result_id") != context["predecessor_result_id"]
            or recovery.get("initial_result_digest") != context["predecessor_result_digest"],
            "daily_capture_recovery_invalid",
        )


def input_artifact_digests(manifest: Mapping[str, object]) -> dict[str, str]:
    inputs = manifest.get("input_artifacts")
    _refuse(
        not isinstance(inputs, Sequence) or isinstance(inputs, str | bytes) or not inputs,
        "daily_manifest_inputs_invalid",
    )
    result: dict[str, str] = {}
    for item in inputs:
        _refuse(
            not isinstance(item, Mapping) or set(item) != {"name", "sha256"},
            "daily_manifest_inputs_invalid",
        )
        name = item["name"]
        _refuse(
            not isinstance(name, str) or not name or name in result, "daily_manifest_inputs_invalid"
        )
        result[name] = _digest(item["sha256"], "daily_manifest_inputs_invalid")
    return result


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
