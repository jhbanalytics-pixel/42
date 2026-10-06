"""Bridge v3 captures recorded on the approval ledger, read beside the daily chain route.

A capture on this route is one consumed ``source_snapshot_capture`` execution under a
trusted generation whose capture policy takes the v3 bridge plan. The approved manifest
digests every input read here, the plan is rebuilt from those inputs at the approval time,
and the clones are bound by the creation records the result names and by the native jobs,
each named for the approved manifest and run by the manifest's own service identity between
consumption and capture. This is a second authority beside the daily chain checks in
``source_estate_bridge``, never a fallback for them, and it changes none of them.

This route admits the initial capture only: the approved vector's mode is ``initial`` and
the recovery context the manifest digests is the canonical null. A recovery is refused here;
recovered captures stay with the daily chain route.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from datetime import UTC, datetime, time, timedelta

from .brain_contract import canonical_bytes, canonical_digest
from .daily_execution_authority import _freeze
from .daily_execution_contracts import _digest, canonical_json_object
from .execution_approval import _parse_timestamp
from .source_estate_bridge import (
    CAPTURE_RESULT_VERSION,
    MARKETS,
    PROFILE_FIELDS,
    PROJECT,
    READ_FIELDS,
    RELATION_FIELDS,
    _check_clones,
    _name,
    _object,
    _observed,
    _pinned_registry_row,
    _require,
    _thaw,
    validate_bridge_profile,
    validate_current_output_set,
)

LEDGER_OPERATION = "source_snapshot_capture"
LEDGER_ARTIFACTS = (
    "bridge_policy",
    "capture_contract",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "recovery_context",
    "source_metadata",
    "storage_policy",
    "temporal_rules",
)
LEDGER_READ_ARTIFACTS = (
    "bridge_policy",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "source_metadata",
    "storage_policy",
    "temporal_rules",
)
PLAN_VERSION = "open_intelligence_protected_capture_plan_v3"
CONSUME_ROUTINE = "sp_consume_open_intelligence_source_snapshot_v3"
STORAGE_VERSION = "open_intelligence_source_capture_storage_v2"
_CODE = "source_bridge_ledger_invalid"
_NOT_INITIAL = "source_bridge_ledger_not_initial"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_SEAL = object()
_ISSUED = weakref.WeakSet()


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


# The digest an initial capture's manifest names for its recovery context: canonical null.
_INITIAL_RECOVERY_CONTEXT = _sha256(canonical_bytes(None))


def _millis(instant):
    return (instant - _EPOCH) // timedelta(milliseconds=1)


def _native_millis(value):
    _require(type(value) is str and re.fullmatch(r"0|[1-9][0-9]{0,15}", value) is not None, _CODE)
    return int(value)


def _capture_rule(registry):
    """The approved generation's capture rule, which must be the v3 bridge rule."""
    from . import execution_approval

    origin = execution_approval._v2_origin_for_operation(
        LEDGER_OPERATION, mode="historical_read", registry=registry
    )
    policy = json.loads(
        execution_approval._policy_bytes_for_origin(registry=registry, origin=origin)
    )
    rule = policy["operation_validation"][LEDGER_OPERATION]
    _require(
        tuple(rule["input_artifact_names"]) == LEDGER_ARTIFACTS
        and rule["arguments"]["plan_contract_version"] == PLAN_VERSION
        and rule["arguments"]["consume_routine"] == CONSUME_ROUTINE,
        _CODE,
    )
    return rule


def is_bridge_ledger_generation(registry):
    """Whether a trusted generation's capture policy is the v3 bridge policy."""
    try:
        _capture_rule(registry)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def ledger_capture_result(ledger_rows, *, consumption_id, generation_loader=None):
    """Decode one consumed bridge capture execution and its complete v3 result payload.

    The approval, consumption and result are typed under the generation pair they record,
    admitted only when ``generation_loader`` trusts it. The cutoff and grant are the ones
    the approved vector names, never the payload's own.
    """
    from . import execution_approval
    from .capture_registry import CAPTURE_JOB_PREFIX
    from .general_question_context_queries import decode_context_result_rows_v2
    from .protected_context_registry import bridge_snapshot_tables
    from .source_estate_bridge_result import _CREATION_FIELDS, _FIELDS

    checked = decode_context_result_rows_v2(
        ledger_rows,
        consumption_id=consumption_id,
        operation=LEDGER_OPERATION,
        generation_loader=generation_loader,
    )
    approval, consumption, result = checked["approval"], checked["consumption"], checked["result"]
    registry = checked["registry"]
    manifest_json = approval.canonical_manifest_json
    _require(
        type(manifest_json) is str
        and _sha256(manifest_json.encode("utf-8"))
        == approval.manifest_sha256
        == consumption.manifest_sha256
        == result["manifest_sha256"],
        _CODE,
    )
    manifest = execution_approval.validate_execution_manifest(
        json.loads(manifest_json), mode="historical_read", registry=registry
    )
    rule = _capture_rule(registry)
    _require(
        result["status"] == "succeeded"
        and result["execution_name"] == consumption.execution_name
        and consumption.job_resource == manifest.job_resource
        and consumption.execution_name.startswith(manifest.job_resource + "/executions/")
        and result["result_reference"] == consumption.execution_name + "#source-snapshot",
        _CODE,
    )
    vector = execution_approval.read_source_snapshot_capture_arguments_v2(
        manifest.arguments, rule=rule["arguments"]
    )
    _require(
        vector.mode == "initial"
        and dict(manifest.input_artifacts).get("recovery_context") == _INITIAL_RECOVERY_CONTEXT,
        _NOT_INITIAL,
    )
    payload, _ = canonical_json_object(result["canonical_result_json"], _CODE)
    day = vector.cutoff_date
    cutoff = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
    _require(
        set(payload) == _FIELDS
        and payload["contract_version"] == CAPTURE_RESULT_VERSION
        and payload["missing_checks"] == []
        and payload["cutoff_date"] == day.isoformat()
        and payload["profile_id"] == f"staging_bridge_v3_{day:%Y%m%d}"
        and payload["grant_id"] == vector.grant_id
        and _parse_timestamp(payload["observation_window_end"], _CODE) == cutoff,
        _CODE,
    )
    relations = payload["relation_bindings"]
    records = payload["creation_records"]
    tables = bridge_snapshot_tables(day)
    _require(
        type(relations) is list
        and type(records) is list
        and len(relations) == len(records) == len(tables),
        _CODE,
    )
    # An initial capture names each creation job for the manifest it was approved under, so
    # a job another execution created cannot be claimed here, even under an identical plan.
    for relation, record, table in zip(relations, records, tables, strict=True):
        _require(
            type(relation) is dict
            and type(record) is dict
            and set(record) == _CREATION_FIELDS
            and relation["destination_table"] == table
            and all(record[key] == relation[key] for key in RELATION_FIELDS)
            and record["state"] == "succeeded"
            and type(record["job_id"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", record["job_id"]) is not None
            and record["job_id"]
            == f"{CAPTURE_JOB_PREFIX}{approval.manifest_sha256}_{record['lane']}",
            _CODE,
        )
        _digest(record["native_job_digest"], _CODE)
    _require(payload["capture_receipt_digest"] == canonical_digest(records), _CODE)
    captured = _parse_timestamp(payload["captured_at"], _CODE)
    _require(consumption.consumed_at <= captured <= result["completed_at"], _CODE)
    _require(
        type(payload["market_scope"]) is list
        and bool(payload["market_scope"])
        and payload["market_scope"] == sorted(set(payload["market_scope"]))
        and set(payload["market_scope"]) <= set(MARKETS),
        _CODE,
    )
    return {
        "approval": approval,
        "consumption": consumption,
        "result": result,
        "manifest": manifest,
        "arguments": vector,
        "payload": payload,
    }


class AdmittedLedgerCapture:
    """A bridge capture the ledger reader admitted; only the reader constructs one."""

    __slots__ = (
        "__weakref__",
        "_seal",
        "approval",
        "arguments",
        "artifacts",
        "consumption",
        "manifest",
        "payload",
        "plan",
        "profile",
        "result",
        "storage_policy",
    )

    def __init__(self, seal, values):
        if seal is not _SEAL:
            raise ValueError(_CODE)
        object.__setattr__(self, "_seal", seal)
        for name in self.__slots__[2:]:
            object.__setattr__(self, name, _freeze(values[name]))
        _ISSUED.add(self)

    def __setattr__(self, name, value):
        raise ValueError("source_bridge_ledger_immutable")

    def __copy__(self):
        raise ValueError("source_bridge_ledger_copy_refused")

    def __deepcopy__(self, memo):
        raise ValueError("source_bridge_ledger_copy_refused")


def read_ledger_capture(*, consumption_id, ledger_rows, generation_loader=None, read_input):
    """Admit one ledger bridge capture from its records and the inputs its manifest digests.

    ``read_input(name, digest)`` returns an input artifact's bytes; each must hash to the
    digest the approved manifest names. The plan is rebuilt from those inputs at the time
    the execution was approved and must match the approved plan byte for byte.
    """
    from src.contracts.open_intelligence import resolve_client_scope

    from .source_estate_bridge_evidence import parse_bridge_artifacts
    from .source_estate_bridge_plan import validate_bridge_plan

    checked = ledger_capture_result(
        ledger_rows, consumption_id=consumption_id, generation_loader=generation_loader
    )
    approval, vector, payload = checked["approval"], checked["arguments"], checked["payload"]
    digests = dict(checked["manifest"].input_artifacts)
    _require(tuple(sorted(digests)) == LEDGER_ARTIFACTS and callable(read_input), _CODE)
    values = {}
    for name in LEDGER_READ_ARTIFACTS:
        raw = read_input(name, digests[name])
        _require(type(raw) is bytes and _sha256(raw) == digests[name], _CODE)
        values[name], _ = canonical_json_object(raw, _CODE)
    plan = values.pop("capture_plan")
    metadata = values.pop("source_metadata")
    storage = values.pop("storage_policy")
    artifacts = parse_bridge_artifacts(values)
    cutoff = vector.cutoff_date.isoformat()
    _require(
        plan.get("contract_version") == PLAN_VERSION
        and plan.get("cutoff_date") == cutoff
        and type(plan.get("client_scope_id")) is str
        and type(plan.get("snapshot_plan")) is dict,
        _CODE,
    )
    _require(
        storage.get("contract_version") == STORAGE_VERSION
        and type(storage.get("grant")) is dict
        and storage["grant"].get("grant_id") == vector.grant_id
        and plan["snapshot_plan"].get("grant_id") == vector.grant_id,
        _CODE,
    )
    scope = resolve_client_scope(
        run_id=LEDGER_OPERATION, client_scope_id=plan["client_scope_id"], market_scope=MARKETS
    )
    expected = validate_bridge_plan(
        plan,
        profile=json.loads(canonical_bytes(plan["snapshot_plan"])),
        source_metadata=metadata,
        storage_policy=storage,
        cutoff_date=cutoff,
        client_scope_id=scope.client_scope_id,
        now=approval.approved_at,
        artifacts=artifacts,
    )
    profile = expected["snapshot_plan"]
    _require(
        all(payload[key] == profile[key] for key in PROFILE_FIELDS - {"schema_digest"})
        and all(
            payload[key] == expected[key]
            for key in ("cutoff_date", "client_scope_id", "market_scope")
        )
        and payload["snapshot_plan_digest"] == canonical_digest(expected),
        _CODE,
    )
    for record, statement in zip(
        payload["creation_records"], expected["creation_statements"], strict=True
    ):
        _require(record["lane"] == statement["lane"], _CODE)
    return AdmittedLedgerCapture(
        _SEAL,
        {
            "approval": approval,
            "arguments": vector,
            "artifacts": artifacts,
            "consumption": checked["consumption"],
            "manifest": checked["manifest"],
            "payload": payload,
            "plan": expected,
            "profile": profile,
            "result": dict(checked["result"]),
            "storage_policy": storage,
        },
    )


def _check_native_job(native, *, record, statement, identity, earliest, latest):
    """One finished CREATE SNAPSHOT TABLE job by ``identity`` inside [earliest, latest]."""
    _require(
        type(native) is dict and record["native_job_digest"] == canonical_digest(native), _CODE
    )
    project, dataset, table = record["destination_table"].split(".")
    status = native.get("status")
    statistics = native.get("statistics")
    configuration = native.get("configuration")
    _require(
        type(status) is dict
        and type(statistics) is dict
        and type(configuration) is dict
        and type(configuration.get("query")) is dict
        and type(statistics.get("query")) is dict,
        _CODE,
    )
    query = statistics["query"]
    _require(
        native.get("jobReference")
        == {"projectId": PROJECT, "location": "US", "jobId": record["job_id"]}
        and native.get("user_email") == identity
        and configuration["query"].get("query") == statement["sql"]
        and status.get("state") == "DONE"
        and status.get("errorResult") is None
        and query.get("statementType") == "CREATE_SNAPSHOT_TABLE"
        and query.get("ddlOperationPerformed") == "CREATE"
        and query.get("ddlTargetTable")
        == {"projectId": project, "datasetId": dataset, "tableId": table},
        _CODE,
    )
    created = _native_millis(statistics.get("creationTime"))
    started = _native_millis(statistics.get("startTime"))
    ended = _native_millis(statistics.get("endTime"))
    _require(_millis(earliest) <= created <= started <= ended <= _millis(latest), _CODE)


def validate_ledger_read_binding(
    value,
    *,
    capture,
    now,
    capture_facts,
    native_jobs,
    clone_metadata,
    operation_id=None,
    current_output_set=None,
):
    """Check a read binding against an admitted ledger capture, the registry and a clock.

    ``capture`` is what ``read_ledger_capture`` issued. The registry row is the committed
    protected context registry entry that pins the ledger result; it is loaded here, never
    supplied by the caller. ``capture_facts``, ``native_jobs`` and ``clone_metadata`` are what
    the reader read natively at request time, checked by the same clone readback as the daily
    chain route; each native job must also be run by the approved manifest's service identity
    between consumption and capture.
    """
    _require(type(capture) is AdmittedLedgerCapture and capture in _ISSUED, _CODE)
    checked = _object(value, READ_FIELDS, "source_bridge_read_binding_invalid")
    _require(
        checked["contract_version"] == "source_bridge_read_binding_v1",
        "source_bridge_read_version_invalid",
    )
    requested = _parse_timestamp(checked["request_as_of"], "source_bridge_request_time_invalid")
    _require(
        requested <= _observed(now, "source_bridge_clock_invalid"),
        "source_bridge_request_after_clock",
    )
    payload = _thaw(capture.payload)
    day = capture.arguments.cutoff_date.isoformat()
    grant = _thaw(capture.storage_policy["grant"])
    profile = validate_bridge_profile(
        _thaw(capture.profile),
        cutoff_date=day,
        observed_at=requested,
        artifacts=_thaw(capture.artifacts),
        source_estate_digest=grant["source_estate_digest"],
    )
    _require(
        profile["grant_id"] == grant["grant_id"] == capture.arguments.grant_id,
        "source_bridge_grant_differs",
    )
    for field in (
        "profile_id",
        "relation_bindings",
        "collection_receipt_set_digest",
        "history_completion_set_digest",
        "temporal_rules_version",
        "temporal_rules_digest",
    ):
        _require(checked[field] == profile[field], "source_bridge_read_profile_differs")
    _require(
        all(payload[field] == profile[field] for field in PROFILE_FIELDS - {"schema_digest"})
        and payload["cutoff_date"] == day,
        "source_bridge_read_result_differs",
    )
    result = _thaw(capture.result)
    _require(
        checked["result_id"] == result["result_id"]
        and checked["result_digest"] == result["result_digest"]
        and result["result_digest"] == _sha256(result["canonical_result_json"].encode("utf-8")),
        "source_bridge_result_id_invalid",
    )
    entry = _pinned_registry_row(result)
    window_end = _parse_timestamp(profile["observation_window_end"], "source_bridge_time_invalid")
    _require(
        checked["registry_entry_digest"] == canonical_digest(entry)
        and entry["cutoff_date"] == day
        and entry["profile_id"] == profile["profile_id"]
        and _observed(entry["source_as_of"], "source_bridge_time_invalid") == window_end
        and entry["market_scope"] == payload["market_scope"]
        and entry["snapshot_tables"]
        == [relation["destination_table"] for relation in profile["relation_bindings"]],
        "source_bridge_registry_entry_differs",
    )
    _require(
        checked["snapshot_digest"] == payload["snapshot_digest"],
        "source_bridge_snapshot_digest_differs",
    )
    _check_clones(
        profile,
        payload,
        capture_facts,
        native_jobs,
        clone_metadata,
        artifacts=_thaw(capture.artifacts),
        source_estate_digest=grant["source_estate_digest"],
    )
    available = _parse_timestamp(checked["available_at"], "source_bridge_available_time_invalid")
    _require(available == result["completed_at"], "source_bridge_available_time_differs")
    snapshot = _parse_timestamp(profile["collection_snapshot_as_of"], "source_bridge_time_invalid")
    captured = _parse_timestamp(payload["captured_at"], "source_bridge_time_invalid")
    _require(snapshot <= captured <= available <= requested, "source_bridge_not_available")
    _require(
        checked["client_scope_id"] == payload["client_scope_id"],
        "source_bridge_client_scope_differs",
    )
    markets = checked["market_scope"]
    _require(
        isinstance(markets, list)
        and bool(markets)
        and all(isinstance(market, str) and market in MARKETS for market in markets),
        "source_bridge_market_scope_invalid",
    )
    _require(markets == sorted(set(markets)), "source_bridge_market_scope_invalid")
    _require(set(markets) <= set(payload["market_scope"]), "source_bridge_market_scope_differs")
    purpose = checked["purpose"]
    _require(
        isinstance(purpose, str)
        and purpose in {"current_context", "daily_collection_cohort", "product_history"},
        "source_bridge_read_purpose_invalid",
    )
    _require(
        checked["product_date"] == (None if purpose == "current_context" else day),
        "source_bridge_product_date_differs",
    )
    current, digest = checked["current_operation_id"], checked["current_output_set_digest"]
    _require((current is None) == (digest is None), "source_bridge_overlay_pair_invalid")
    if current is None:
        _require(
            operation_id is None and current_output_set is None,
            "source_bridge_ambient_output_invalid",
        )
    else:
        _require(purpose != "current_context", "source_bridge_context_overlay_invalid")
        _name(current, "source_bridge_operation_invalid")
        _require(current == operation_id, "source_bridge_operation_differs")
        output = validate_current_output_set(current_output_set, operation_id=operation_id)
        _digest(digest, "source_bridge_output_digest_invalid")
        _require(digest == canonical_digest(output), "source_bridge_output_set_differs")
    # The clones: every creation record names one native job by the approved identity.
    identity = capture.manifest.service_identity
    statements = _thaw(capture.plan["creation_statements"])
    for record, statement in zip(payload["creation_records"], statements, strict=True):
        _check_native_job(
            native_jobs[record["job_id"]],
            record=record,
            statement=statement,
            identity=identity,
            earliest=capture.consumption.consumed_at,
            latest=captured,
        )
    return checked
