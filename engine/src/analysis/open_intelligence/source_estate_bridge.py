"""Pure bridge consistency checks, never independent execution authority.

Profile digests are recomputed from the parsed bridge artifacts they name, the estate
digest is compared with the grant value the caller read, and a read binding is checked
against an issued daily capture chain, its registry entry and the caller's clock. Native
readers still establish each artifact's own provenance with the evidence resolvers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta

from .brain_contract import canonical_digest
from .daily_execution_authority import _ISSUED_CHAINS, AdmittedDailyExecutionChain
from .daily_execution_contracts import _digest, _instant, canonical_json_object
from .execution_approval import _format_timestamp, _parse_timestamp
from .geographic_scope import bounded_name, bounded_text
from .recurring_grant import _GRANT_ID, grant_digest, validate_recurring_grant

PROJECT = "ogilvy-trends-v2"
# The collection lanes copy what the funded Wave 1 pilot writes to trends_v2_staging. Daily
# free collection into intelligence_42_sources_staging is not a source of these lanes.
COLLECTION_DATASET = "trends_v2_staging"
PRODUCT_DATASET = "trends_v2_staging"
COLLECTION_TABLES = ("enriched_content", "raw_content")
HISTORY_TABLES = (
    "event_ledger",
    "seed_candidates",
    "seed_graph",
    "trend_analysis",
    "trend_scores",
)
LANES = tuple(sorted((*COLLECTION_TABLES, *HISTORY_TABLES)))
MARKETS = ("ke", "ng", "za")
PROFILE_VERSION = "42_staging_source_bridge_v3"
PROJECTION_VERSION = "native_id_bound_v1"
TEMPORAL_VERSION = "source_observation_semantics_v1"
PROFILE_FIELDS = frozenset(
    [
        "profile_id",
        "profile_version",
        "projection_version",
        "observation_window_end",
        "source_estate_digest",
        "grant_id",
        "schema_digest",
        "bridge_policy_digest",
        "temporal_rules_version",
        "temporal_rules_digest",
        "collection_snapshot_as_of",
        "history_snapshot_as_of",
        "collection_receipt_set_digest",
        "history_completion_set_digest",
        "relation_bindings",
    ]
)
RELATION_FIELDS = frozenset(
    ["lane", "role", "source_table", "destination_table", "snapshot_as_of", "source_schema_digest"]
)
FACT_FIELDS = frozenset(
    [
        "contract_version",
        "profile_digest",
        "relation_readbacks",
        "collection_receipt_set_digest",
        "history_completion_set_digest",
        "projection_version",
        "temporal_rules_version",
        "temporal_rules_digest",
        "captured_at",
    ]
)
READBACK_FIELDS = frozenset(
    [
        "lane",
        "destination_table",
        "snapshot_as_of",
        "source_schema_digest",
        "row_count",
        "metadata_digest",
    ]
)
OUTPUT_FIELDS = frozenset(
    ["lane", "write_job_reference", "write_job_digest", "natural_key_set_digest", "readback_digest"]
)
READ_FIELDS = frozenset(
    [
        "contract_version",
        "registry_entry_digest",
        "profile_id",
        "result_id",
        "result_digest",
        "snapshot_digest",
        "request_as_of",
        "purpose",
        "product_date",
        "client_scope_id",
        "market_scope",
        "relation_bindings",
        "collection_receipt_set_digest",
        "history_completion_set_digest",
        "temporal_rules_version",
        "temporal_rules_digest",
        "available_at",
        "current_operation_id",
        "current_output_set_digest",
    ]
)


REGISTRY_PINS = ("consumption_id", "manifest_sha256", "result_digest", "result_id")
REGISTRY_FIELDS = (
    "consumption_id",
    "cutoff_date",
    "manifest_sha256",
    "market_scope",
    "profile_id",
    "result_digest",
    "result_id",
    "snapshot_tables",
    "source_as_of",
)
CAPTURE_OPERATION = "daily_source_snapshot_capture"
CAPTURE_RESULT_VERSION = "open_intelligence_protected_source_snapshot_v3"


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _object(value, fields, code):
    try:
        record, raw = canonical_json_object(value, code)
    except (TypeError, UnicodeError, OverflowError) as error:
        raise ValueError(code) from error
    _require(set(record) == fields, code)
    return json.loads(raw)


def _name(value, code):
    _require(bounded_name(value, code) == value, code)
    return value


def _observed(value, code):
    if isinstance(value, datetime):
        _require(value.tzinfo is not None and value.utcoffset() is not None, code)
        return _parse_timestamp(_format_timestamp(value, code), code)
    return _instant(value, code)


def _day(value):
    code = "source_bridge_cutoff_invalid"
    _require(isinstance(value, str), code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(code) from error
    _require(parsed.isoformat() == value, code)
    return parsed


def _profile_day(profile):
    profile = _object(profile, PROFILE_FIELDS, "source_bridge_profile_invalid")
    end = _parse_timestamp(profile["observation_window_end"], "source_bridge_time_invalid")
    return (end - timedelta(days=1)).date().isoformat()


def bridge_policy():
    """Return the fixed declaration as fresh JSON data, without approval facts."""
    return {
        "contract_version": "42_source_estate_bridge_v1",
        "collection_dataset": COLLECTION_DATASET,
        "product_dataset": PRODUCT_DATASET,
        "snapshot_dataset": PRODUCT_DATASET,
        "collection_tables": list(COLLECTION_TABLES),
        "history_tables": list(HISTORY_TABLES),
        "market_scope": list(MARKETS),
        "window_rule": "completed_run_availability_with_closed_collection_day_products_v1",
        "temporal_rules_version": TEMPORAL_VERSION,
        "history_read_rule": "prior_completed_products_as_of_cutoff_v1",
    }


def validate_bridge_policy(value):
    expected = bridge_policy()
    checked = _object(value, set(expected), "source_bridge_policy_invalid")
    _require(checked == expected, "source_bridge_policy_invalid")
    return checked


def _artifact_bindings(checked, artifacts, *, cutoff, day, available):
    from .source_estate_bridge_evidence import parse_bridge_artifacts

    try:
        parsed = parse_bridge_artifacts(artifacts)
    except (TypeError, ValueError) as error:
        raise ValueError("source_bridge_artifacts_invalid") from error
    for field, name in (
        ("bridge_policy_digest", "bridge_policy"),
        ("temporal_rules_digest", "temporal_rules"),
        ("collection_receipt_set_digest", "collection_receipt_set"),
        ("history_completion_set_digest", "history_completion_set"),
    ):
        _require(
            checked[field] == canonical_digest(parsed[name]),
            "source_bridge_artifact_digest_differs",
        )
    # Capture after collection is checked for every receipted run, never only when supplied.
    for receipt in parsed["collection_receipt_set"]["receipts"]:
        _require(
            _parse_timestamp(receipt["collection_completed_at"], "source_bridge_time_invalid")
            <= available,
            "source_bridge_capture_precedes_collection",
        )
    entries = parsed["history_completion_set"]["entries"]
    last = max(entry["product_date"] for entry in entries)
    _require(last == day.isoformat(), "source_bridge_history_window_invalid")
    for entry in entries:
        if entry["state"] != "unavailable":
            _require(
                _parse_timestamp(entry["available_at"], "source_bridge_time_invalid") <= cutoff,
                "source_bridge_history_window_invalid",
            )
    return parsed


def validate_bridge_profile(
    value, *, cutoff_date, observed_at, artifacts, source_estate_digest, collection_completed_at=()
):
    checked = _object(value, PROFILE_FIELDS, "source_bridge_profile_invalid")
    day = _day(cutoff_date)
    cutoff = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
    _require(
        checked["profile_id"] == f"staging_bridge_v3_{day:%Y%m%d}"
        and checked["profile_version"] == PROFILE_VERSION
        and checked["projection_version"] == PROJECTION_VERSION
        and checked["temporal_rules_version"] == TEMPORAL_VERSION,
        "source_bridge_profile_version_invalid",
    )
    for field in (
        "source_estate_digest",
        "schema_digest",
        "bridge_policy_digest",
        "temporal_rules_digest",
        "collection_receipt_set_digest",
        "history_completion_set_digest",
    ):
        _digest(checked[field], "source_bridge_digest_invalid")
    _require(
        isinstance(checked["grant_id"], str)
        and _GRANT_ID.fullmatch(checked["grant_id"]) is not None,
        "source_bridge_grant_name_invalid",
    )
    _require(
        checked["bridge_policy_digest"] == canonical_digest(bridge_policy()),
        "source_bridge_policy_differs",
    )
    _require(
        checked["source_estate_digest"]
        == _digest(source_estate_digest, "source_bridge_estate_invalid"),
        "source_bridge_estate_differs",
    )
    closed = _parse_timestamp(checked["observation_window_end"], "source_bridge_time_invalid")
    history = _parse_timestamp(checked["history_snapshot_as_of"], "source_bridge_time_invalid")
    available = _parse_timestamp(checked["collection_snapshot_as_of"], "source_bridge_time_invalid")
    _require(
        closed == history == cutoff
        and cutoff <= available <= _observed(observed_at, "source_bridge_time_invalid"),
        "source_bridge_chronology_invalid",
    )
    _artifact_bindings(checked, artifacts, cutoff=cutoff, day=day, available=available)
    _require(
        isinstance(collection_completed_at, list | tuple), "source_bridge_completion_times_invalid"
    )
    for completed in collection_completed_at:
        _require(
            _observed(completed, "source_bridge_completion_times_invalid") <= available,
            "source_bridge_capture_precedes_collection",
        )
    relations = checked["relation_bindings"]
    _require(
        isinstance(relations, list) and len(relations) == len(LANES),
        "source_bridge_relations_inexact",
    )
    schemas = []
    for lane, value in zip(LANES, relations, strict=True):
        _require(type(value) is dict, "source_bridge_relation_invalid")
        relation = _object(value, RELATION_FIELDS, "source_bridge_relation_invalid")
        collection = lane in COLLECTION_TABLES
        dataset = COLLECTION_DATASET if collection else PRODUCT_DATASET
        stamp = (
            checked["collection_snapshot_as_of"]
            if collection
            else checked["history_snapshot_as_of"]
        )
        _require(
            relation["lane"] == lane
            and relation["role"] == ("collection_evidence" if collection else "derived_history")
            and relation["source_table"] == f"{PROJECT}.{dataset}.{lane}"
            and relation["destination_table"]
            == f"{PROJECT}.{PRODUCT_DATASET}.{checked['profile_id']}_{lane}"
            and relation["snapshot_as_of"] == stamp,
            "source_bridge_relation_differs",
        )
        _digest(relation["source_schema_digest"], "source_bridge_schema_invalid")
        schemas.append(
            {key: relation[key] for key in ("lane", "source_table", "source_schema_digest")}
        )
    _require(
        checked["schema_digest"] == canonical_digest(schemas), "source_bridge_schema_digest_differs"
    )
    return checked


def validate_capture_facts(
    value,
    *,
    profile,
    artifacts,
    source_estate_digest,
    creation_completed_at,
    stored_created_at=None,
    result_captured_at=None,
    attempt_captured_at=None,
):
    checked = _object(value, FACT_FIELDS, "source_bridge_capture_facts_invalid")
    _require(
        checked["contract_version"] == "open_intelligence_source_bridge_capture_v1",
        "source_bridge_capture_version_invalid",
    )
    captured = _parse_timestamp(checked["captured_at"], "source_bridge_capture_time_invalid")
    profile = validate_bridge_profile(
        profile,
        cutoff_date=_profile_day(profile),
        observed_at=captured,
        artifacts=artifacts,
        source_estate_digest=source_estate_digest,
    )
    _require(
        checked["profile_digest"] == canonical_digest(profile),
        "source_bridge_capture_profile_differs",
    )
    for field in (
        "collection_receipt_set_digest",
        "history_completion_set_digest",
        "projection_version",
        "temporal_rules_version",
        "temporal_rules_digest",
    ):
        _require(checked[field] == profile[field], "source_bridge_capture_binding_differs")
    _require(
        isinstance(creation_completed_at, dict) and set(creation_completed_at) == set(LANES),
        "source_bridge_creation_times_inexact",
    )
    for completed in creation_completed_at.values():
        _require(
            _observed(completed, "source_bridge_creation_time_invalid") <= captured,
            "source_bridge_capture_precedes_creation",
        )
    readbacks = checked["relation_readbacks"]
    _require(
        isinstance(readbacks, list) and len(readbacks) == len(LANES),
        "source_bridge_readbacks_inexact",
    )
    for relation, value in zip(profile["relation_bindings"], readbacks, strict=True):
        _require(type(value) is dict, "source_bridge_readback_invalid")
        readback = _object(value, READBACK_FIELDS, "source_bridge_readback_invalid")
        _require(
            all(
                readback[key] == relation[key]
                for key in ("lane", "destination_table", "snapshot_as_of", "source_schema_digest")
            ),
            "source_bridge_readback_differs",
        )
        _require(
            type(readback["row_count"]) is int and readback["row_count"] >= 0,
            "source_bridge_row_count_invalid",
        )
        _digest(readback["metadata_digest"], "source_bridge_metadata_digest_invalid")
    supplied = (stored_created_at, result_captured_at, attempt_captured_at)
    if any(item is not None for item in supplied):
        _require(all(item is not None for item in supplied), "source_bridge_storage_times_inexact")
        for value in (result_captured_at, attempt_captured_at):
            _parse_timestamp(value, "source_bridge_capture_time_invalid")
            _require(value == checked["captured_at"], "source_bridge_capture_time_differs")
        _require(
            captured <= _observed(stored_created_at, "source_bridge_storage_time_invalid"),
            "source_bridge_storage_precedes_capture",
        )
    return checked


def validate_current_output_set(value, *, operation_id):
    code = "source_bridge_current_outputs_invalid"
    checked = _object(value, {"contract_version", "operation_id", "outputs"}, code)
    _name(operation_id, "source_bridge_operation_invalid")
    _require(
        checked["contract_version"] == "42_bridge_current_output_set_v1"
        and checked["operation_id"] == operation_id,
        "source_bridge_operation_differs",
    )
    _require(isinstance(checked["outputs"], list), code)
    keys = []
    for value in checked["outputs"]:
        _require(type(value) is dict, code)
        output = _object(value, OUTPUT_FIELDS, code)
        _require(output["lane"] in HISTORY_TABLES, "source_bridge_output_lane_invalid")
        reference = bounded_text(
            output["write_job_reference"], "source_bridge_write_reference_invalid"
        )
        _require(
            reference == output["write_job_reference"], "source_bridge_write_reference_invalid"
        )
        for field in ("write_job_digest", "natural_key_set_digest", "readback_digest"):
            _digest(output[field], "source_bridge_output_digest_invalid")
        keys.append((output["lane"], reference))
    _require(keys == sorted(set(keys)), "source_bridge_outputs_order_invalid")
    return checked


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


def _capture_result(chain):
    """Return the retained capture result carried by an issued native daily chain."""
    code = "source_bridge_capture_chain_invalid"
    _require(type(chain) is AdmittedDailyExecutionChain and chain in _ISSUED_CHAINS, code)
    _require(
        chain.operation_context["operation"] == CAPTURE_OPERATION
        and chain.result["operation"] == CAPTURE_OPERATION
        and chain.result["status"] == "succeeded",
        code,
    )
    _require(chain.result["result_id"] == "exr_" + chain.result["result_digest"], code)
    from .source_estate_bridge_result import _FIELDS

    payload = _thaw(chain.operation_payload)
    _require(
        set(payload) == _FIELDS
        and payload["contract_version"] == CAPTURE_RESULT_VERSION
        and payload["missing_checks"] == [],
        code,
    )
    return payload


def _pinned_registry_row(result):
    """Return the one committed protected context registry row that pins this result."""
    from .protected_context_registry import load_protected_context_registry

    code = "source_bridge_registry_entry_differs"
    rows = [
        {
            name: list(getattr(entry, name))
            if isinstance(getattr(entry, name), tuple)
            else getattr(entry, name)
            for name in REGISTRY_FIELDS
        }
        for entry in load_protected_context_registry()
        if entry.result_id is not None and entry.result_id == result["result_id"]
    ]
    _require(len(rows) == 1, code)
    row = rows[0]
    _require(all(row[name] == result[name] for name in REGISTRY_PINS), code)
    return row


def _authorizing_grant(grant, chain):
    """The recurring grant whose digest authorized the capture chain."""
    code = "source_bridge_grant_differs"
    try:
        checked = validate_recurring_grant(grant)
        digest = grant_digest(checked)
    except ValueError as error:
        raise ValueError(code) from error
    _require(
        digest == chain.operation_context["authorizing_grant_digest"]
        and "immutable_capture" in checked["allowed_operations"],
        code,
    )
    return checked


CLONE_FINGERPRINT_FIELDS = (
    "tableReference",
    "type",
    "snapshotDefinition",
    "creationTime",
    "numRows",
    "schema",
)


def clone_fingerprint(resource):
    """The content fingerprint a capture records for one clone's tables.get resource.

    Volatile metadata (etag, lastModifiedTime, storage byte counts) is left out; every field
    that a delete and recreate under the same name would change is kept. The plan never sets
    an expiry, so a clone that carries one is refused rather than fingerprinted.
    """
    code = "source_bridge_clone_invalid"
    _require(
        type(resource) is dict
        and set(CLONE_FINGERPRINT_FIELDS) <= set(resource)
        and "expirationTime" not in resource,
        code,
    )
    return {name: resource[name] for name in CLONE_FINGERPRINT_FIELDS}


def _millis_instant(value, code):
    _require(type(value) is str and value.isdecimal(), code)
    return int(value)


def _table_reference(name):
    project, dataset, table = name.split(".")
    return {"projectId": project, "datasetId": dataset, "tableId": table}


def _check_clones(profile, payload, capture_facts, native_jobs, clone_metadata, **facts_inputs):
    """Read back each clone against the jobs and the fingerprints the capture recorded."""
    code = "source_bridge_clone_differs"
    relations = profile["relation_bindings"]
    records = payload["creation_records"]
    _require(type(records) is list and len(records) == len(relations), code)
    jobs = {}
    for record, relation in zip(records, relations, strict=True):
        _require(
            type(record) is dict
            and all(record.get(key) == relation[key] for key in RELATION_FIELDS)
            and record.get("state") == "succeeded"
            and type(record.get("job_id")) is str,
            code,
        )
        jobs[relation["lane"]] = record
    _require(
        type(native_jobs) is dict
        and set(native_jobs) == {record["job_id"] for record in records}
        and len(native_jobs) == len(records),
        code,
    )
    windows = {}
    for lane, record in jobs.items():
        # A lookup by a key the job set lacks is a refusal, never a KeyError.
        native = native_jobs.get(record["job_id"])
        _require(type(native) is dict, code)
        _require(canonical_digest(native) == record["native_job_digest"], code)
        reference = native.get("jobReference")
        _require(type(reference) is dict and reference.get("jobId") == record["job_id"], code)
        statistics = native.get("statistics")
        _require(type(statistics) is dict and type(statistics.get("query")) is dict, code)
        # The job must have created this lane's clone, so two records cannot trade jobs.
        _require(
            statistics["query"].get("ddlTargetTable")
            == _table_reference(record["destination_table"]),
            code,
        )
        windows[lane] = (
            _millis_instant(statistics.get("creationTime"), code),
            _millis_instant(statistics.get("endTime"), code),
        )
    facts = validate_capture_facts(
        capture_facts,
        profile=profile,
        creation_completed_at={
            lane: datetime.fromtimestamp(end / 1000, UTC) for lane, (_, end) in windows.items()
        },
        **facts_inputs,
    )
    _require(
        canonical_digest(facts) == payload["snapshot_digest"]
        and facts["captured_at"] == payload["captured_at"],
        code,
    )
    _check_clone_readbacks(relations, facts["relation_readbacks"], clone_metadata, windows, code)


def _check_clone_readbacks(relations, readbacks, clone_metadata, windows, code):
    """Recompute each clone's fingerprint from its tables.get resource.

    ``windows`` maps each lane to its creation job's (creationTime, endTime) in epoch millis,
    and each clone must have been created inside its own job's window.
    """
    _require(
        type(clone_metadata) is dict
        and set(clone_metadata) == {relation["destination_table"] for relation in relations},
        code,
    )
    for relation, readback in zip(relations, readbacks, strict=True):
        resource = clone_metadata.get(relation["destination_table"])
        _require(
            type(resource) is dict
            and set(CLONE_FINGERPRINT_FIELDS) <= set(resource)
            and "expirationTime" not in resource,
            code,
        )
        fingerprint = clone_fingerprint(resource)
        definition = fingerprint["snapshotDefinition"]
        _require(type(definition) is dict and type(definition.get("snapshotTime")) is str, code)
        try:
            snapshot_time = datetime.fromisoformat(definition["snapshotTime"])
        except ValueError as error:
            raise ValueError(code) from error
        start, end = windows[relation["lane"]]
        _require(
            fingerprint["tableReference"] == _table_reference(relation["destination_table"])
            and fingerprint["type"] == "SNAPSHOT"
            and definition.get("baseTableReference") == _table_reference(relation["source_table"])
            and snapshot_time.tzinfo is not None
            and snapshot_time == _parse_timestamp(relation["snapshot_as_of"], code)
            and start <= _millis_instant(fingerprint["creationTime"], code) <= end
            and fingerprint["numRows"] == str(readback["row_count"])
            and canonical_digest(fingerprint) == readback["metadata_digest"],
            code,
        )


def validate_source_read_binding(
    value,
    *,
    profile,
    artifacts,
    capture_chain,
    grant,
    now,
    capture_facts,
    native_jobs,
    clone_metadata,
    operation_id=None,
    current_output_set=None,
):
    """Check a read binding against the issued capture chain, the registry, a grant and a clock.

    ``capture_chain`` is the chain ``read_daily_execution_chain`` issued for the capture
    operation, ``grant`` is the recurring grant whose digest authorized that chain, and ``now``
    is the reader's own clock. The registry row is the committed protected context registry
    entry that pins the chain's result; it is loaded here, never supplied by the caller.
    ``capture_facts`` is the stored capture artifact's facts, ``native_jobs`` the jobs.get
    resources of the creation jobs and ``clone_metadata`` the tables.get resource of every
    clone, read at request time, so a clone replaced after capture is refused.
    """
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
    payload = _capture_result(capture_chain)
    authorizing = _authorizing_grant(grant, capture_chain)
    day = _profile_day(profile)
    profile = validate_bridge_profile(
        profile,
        cutoff_date=day,
        observed_at=requested,
        artifacts=artifacts,
        source_estate_digest=authorizing["source_policy_digest"],
    )
    _require(profile["grant_id"] == authorizing["grant_id"], "source_bridge_grant_differs")
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
    _require(
        _observed(capture_chain.operation_context["cutoff_utc"], "source_bridge_time_invalid")
        == _parse_timestamp(profile["observation_window_end"], "source_bridge_time_invalid"),
        "source_bridge_read_result_differs",
    )
    result = capture_chain.result
    _require(
        checked["result_id"] == result["result_id"]
        and checked["result_digest"] == result["result_digest"]
        and checked["result_id"] == "exr_" + checked["result_digest"],
        "source_bridge_result_id_invalid",
    )
    entry = _pinned_registry_row(result)
    _require(
        checked["registry_entry_digest"] == canonical_digest(entry)
        and entry["cutoff_date"] == day
        and _observed(entry["source_as_of"], "source_bridge_time_invalid")
        == _parse_timestamp(profile["observation_window_end"], "source_bridge_time_invalid")
        and entry["market_scope"] == payload["market_scope"],
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
        artifacts=artifacts,
        source_estate_digest=authorizing["source_policy_digest"],
    )
    available = _parse_timestamp(checked["available_at"], "source_bridge_available_time_invalid")
    _require(
        available == _observed(capture_chain.effective_available_at, "source_bridge_time_invalid"),
        "source_bridge_available_time_differs",
    )
    capture = _parse_timestamp(profile["collection_snapshot_as_of"], "source_bridge_time_invalid")
    captured = _parse_timestamp(payload["captured_at"], "source_bridge_time_invalid")
    _require(capture <= captured <= available <= requested, "source_bridge_not_available")
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
    return checked
