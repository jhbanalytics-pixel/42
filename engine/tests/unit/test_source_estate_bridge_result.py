"""Unissued result comparisons do not grant native capture authority."""

from copy import deepcopy
from datetime import datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.production_snapshot_storage import BUCKET_NAME
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit.source_bridge_chain_fixture import capture_consumption
from tests.unit.test_source_estate_bridge_contract import (
    CAPTURED,
    clone_fingerprint_digest,
    clone_resource,
    facts,
    synthetic_digest,
)

IDENTITY = "service@example.iam.gserviceaccount.com"
from tests.unit.test_source_estate_bridge_plan import inputs


def fixture():
    plan_inputs = inputs()
    plan = build_bridge_plan(**plan_inputs)
    profile = plan["snapshot_plan"]
    capture = facts()
    capture["profile_digest"] = canonical_digest(profile)
    for readback, relation in zip(
        capture["relation_readbacks"], profile["relation_bindings"], strict=True
    ):
        readback["source_schema_digest"] = relation["source_schema_digest"]
    jobs = {
        "capture_" + statement["lane"]: native_job(statement, relation)
        for statement, relation in zip(
            plan["creation_statements"], profile["relation_bindings"], strict=True
        )
    }
    records = [
        {
            **relation,
            "job_id": "capture_" + relation["lane"],
            "native_job_digest": canonical_digest(jobs["capture_" + relation["lane"]]),
            "state": "succeeded",
        }
        for relation in profile["relation_bindings"]
    ]
    digest = synthetic_digest("unissued stored bytes")
    attempt = {
        "uri": f"gs://{BUCKET_NAME}/captures/{'a' * 64}/{digest}/capture.json",
        "size_bytes": 100,
        "sha256": digest,
        "captured_at": CAPTURED,
    }
    stored = {key: attempt[key] for key in ("uri", "size_bytes", "sha256")}
    stored.update(generation=1, created_at="2026-09-21T00:26:00.000000Z")
    result = {key: deepcopy(value) for key, value in profile.items() if key != "schema_digest"}
    result.update(
        contract_version="open_intelligence_protected_source_snapshot_v3",
        cutoff_date=plan["cutoff_date"],
        client_scope_id=plan["client_scope_id"],
        market_scope=plan["market_scope"],
        captured_at=CAPTURED,
        snapshot_plan_digest=canonical_digest(plan),
        snapshot_digest=canonical_digest(capture),
        capture_receipt_digest=canonical_digest(records),
        creation_records=deepcopy(records),
        artifact_attempt=deepcopy(attempt),
        stored_artifact=deepcopy(stored),
        query_count=7,
        total_bytes_billed=0,
        limitations=["historical_predecessor_unavailable"],
        missing_checks=[],
    )
    context = {
        "plan": plan,
        "plan_inputs": plan_inputs,
        "capture_facts": capture,
        "creation_records": records,
        "creation_completed_at": {row["lane"]: CAPTURED for row in records},
        "artifact_attempt": attempt,
        "stored_artifact": stored,
        "execution_status": "succeeded",
        "native_metering": {"query_count": 7, "total_bytes_billed": 0},
        "native_jobs": jobs,
        "capture_consumption": consumption_for(profile),
        "clone_metadata": {
            relation["destination_table"]: clone_resource(relation)
            for relation in profile["relation_bindings"]
        },
    }
    return result, context


def consumption_for(profile, **changes):
    terms = {
        "cutoff_utc": "2026-09-21T00:00:00+00:00",
        "grant_id": profile["grant_id"],
        # The estate digest is the digest of these source policy bytes.
        "source_policy": canonical_bytes({"unissued_test_fixture": "connector inventory"}),
    }
    return capture_consumption(**{**terms, **changes})


def _millis(value):
    return str(int(datetime.fromisoformat(value).timestamp() * 1000))


def native_job(statement, relation):
    project, dataset, table = relation["destination_table"].split(".")
    return {
        "jobReference": {
            "projectId": "ogilvy-trends-v2",
            "location": "US",
            "jobId": "capture_" + statement["lane"],
        },
        "configuration": {"query": {"query": statement["sql"], "useLegacySql": False}},
        "status": {"state": "DONE"},
        "user_email": IDENTITY,
        "statistics": {
            "creationTime": _millis("2026-09-21T00:21:00+00:00"),
            "endTime": _millis(CAPTURED),
            "query": {
                "statementType": "CREATE_SNAPSHOT_TABLE",
                "ddlOperationPerformed": "CREATE",
                "ddlTargetTable": {"projectId": project, "datasetId": dataset, "tableId": table},
                "totalBytesBilled": "0",
            },
        },
    }


def check(value, context):
    from src.analysis.open_intelligence.source_estate_bridge_result import (
        validate_successful_bridge_result,
    )

    return validate_successful_bridge_result(value, **context)


def test_successful_result_roundtrip_retains_historical_limitation_and_detaches():
    result, context = fixture()
    checked = check(canonical_bytes(result), context)
    assert checked == result
    checked["creation_records"].clear()
    assert len(result["creation_records"]) == len(context["creation_records"]) == 7


def test_dictionary_input_is_detached_recursively():
    result, context = fixture()
    checked = check(result, context)
    checked["creation_records"][0]["state"] = "failed"
    checked["stored_artifact"]["generation"] = 123
    assert result["creation_records"][0]["state"] == "succeeded"
    assert result["stored_artifact"]["generation"] == 1


def test_tuple_creation_records_cannot_pass_through_json_serialization():
    result, context = fixture()
    result["creation_records"] = tuple(result["creation_records"])
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "open_intelligence_protected_source_snapshot_v2"),
        ("query_count", True),
        ("total_bytes_billed", False),
        ("query_count", -1),
        ("total_bytes_billed", 1.5),
        ("query_count", None),
        ("missing_checks", ["missing_native_readback"]),
        ("limitations", "unknown"),
        ("limitations", [True]),
        ("captured_at", "2026-09-21T00:24:00.000000Z"),
        ("snapshot_digest", "0" * 64),
        ("capture_receipt_digest", "0" * 64),
        ("client_scope_id", "foreign"),
        ("market_scope", ["za"]),
        ("source_estate_digest", "0" * 64),
        ("relation_bindings", []),
    ],
)
def test_result_drift_is_refused(field, value):
    result, context = fixture()
    result[field] = value
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize(
    "defect",
    [
        "extra",
        "encoded_record",
        "missing_lane",
        "duplicate_lane",
        "forged_job",
        "forged_readback",
        "forged_sql",
        "failed_job",
        "late_completion",
        "stored_generation",
        "stored_size_bool",
        "attempt_time",
        "stored_time",
    ],
)
def test_self_consistent_candidate_cannot_replace_comparison_evidence(defect):
    result, context = fixture()
    if defect == "extra":
        result["approved"] = True
    elif defect == "encoded_record":
        result["creation_records"][0] = canonical_bytes(result["creation_records"][0]).decode()
    elif defect == "missing_lane":
        result["creation_records"].pop()
    elif defect == "duplicate_lane":
        result["creation_records"][-1] = deepcopy(result["creation_records"][0])
    elif defect == "forged_job":
        result["creation_records"][0]["native_job_digest"] = "0" * 64
    elif defect == "failed_job":
        context["creation_records"][0]["state"] = "failed"
        result["creation_records"] = deepcopy(context["creation_records"])
    elif defect == "forged_readback":
        result["snapshot_digest"] = canonical_digest({"relation_readbacks": []})
    elif defect == "forged_sql":
        context["plan"]["creation_statements"][0]["sql"] = "SELECT 1"
        result["snapshot_plan_digest"] = canonical_digest(context["plan"])
    elif defect == "late_completion":
        context["creation_completed_at"]["raw_content"] = "2026-09-21T00:27:00.000000Z"
    elif defect == "stored_generation":
        result["stored_artifact"]["generation"] = 2
    elif defect == "stored_size_bool":
        result["stored_artifact"]["size_bytes"] = True
    elif defect == "attempt_time":
        result["artifact_attempt"]["captured_at"] = "2026-09-21T00:24:00.000000Z"
    else:
        context["stored_artifact"]["created_at"] = "2026-09-21T00:24:00.000000Z"
        result["stored_artifact"] = deepcopy(context["stored_artifact"])
    result["capture_receipt_digest"] = canonical_digest(result["creation_records"])
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize("status", ["failed", "unknown", None, True])
def test_only_successful_outer_results_reach_this_boundary(status):
    result, context = fixture()
    context["execution_status"] = status
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize("field,value", [("query_count", 8), ("total_bytes_billed", 100)])
def test_valid_numbers_must_equal_independent_native_metering(field, value):
    result, context = fixture()
    result[field] = value
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize(
    "metering",
    [
        None,
        {"query_count": True, "total_bytes_billed": 0},
        {"query_count": 7},
        {"query_count": 7, "total_bytes_billed": -1},
    ],
)
def test_incomplete_or_invalid_native_metering_refuses(metering):
    result, context = fixture()
    context["native_metering"] = metering
    with pytest.raises(ValueError):
        check(result, context)


@pytest.mark.parametrize(
    "defect",
    [
        "job_digest",
        "missing_job",
        "extra_job",
        "job_reference",
        "native_sql",
        "native_error",
        "native_running",
        "ddl_target",
        "not_created",
        "end_time",
        "principal",
        "collection_consumption",
        "unsealed_consumption",
        "consumption_cutoff",
        "consumption_grant",
        "consumption_estate",
    ],
)
def test_native_job_digest_is_recomputed_from_the_native_job_it_names(defect):
    result, context = fixture()
    jobs = context["native_jobs"]
    job_id = context["creation_records"][0]["job_id"]
    native = jobs[job_id]
    if defect == "job_digest":
        context["creation_records"][0]["native_job_digest"] = synthetic_digest("forged job")
    elif defect == "missing_job":
        del jobs[job_id]
    elif defect == "extra_job":
        jobs["capture_retry"] = deepcopy(native)
    elif defect == "job_reference":
        native["jobReference"]["jobId"] = "capture_other"
    elif defect == "native_sql":
        native["configuration"]["query"]["query"] = "SELECT 1"
    elif defect == "native_error":
        native["status"]["errorResult"] = {"reason": "invalidQuery"}
    elif defect == "native_running":
        native["status"]["state"] = "RUNNING"
    elif defect == "ddl_target":
        native["statistics"]["query"]["ddlTargetTable"]["tableId"] += "_other"
    elif defect == "not_created":
        native["statistics"]["query"]["ddlOperationPerformed"] = "SKIP"
    elif defect == "end_time":
        native["statistics"]["endTime"] = _millis("2026-09-21T00:24:00+00:00")
    elif defect == "principal":
        native["user_email"] = "intelligence-42-ingest@ogilvy-trends-v2.iam.gserviceaccount.com"
    elif defect == "collection_consumption":
        context["capture_consumption"] = capture_consumption("daily_source_collection")
    elif defect == "consumption_cutoff":
        context["capture_consumption"] = consumption_for(
            context["plan"]["snapshot_plan"], cutoff_utc="2026-09-20T00:00:00+00:00"
        )
    elif defect == "consumption_grant":
        context["capture_consumption"] = consumption_for(
            context["plan"]["snapshot_plan"], grant_id="another_test_grant"
        )
    elif defect == "consumption_estate":
        context["capture_consumption"] = consumption_for(
            context["plan"]["snapshot_plan"], source_policy=b"another source policy"
        )
    else:
        from types import SimpleNamespace

        admitted = context["capture_consumption"]
        context["capture_consumption"] = SimpleNamespace(
            operation=admitted.operation, operation_context=admitted.operation_context
        )
    if defect not in {"job_digest", "missing_job", "extra_job"}:
        context["creation_records"][0]["native_job_digest"] = canonical_digest(native)
    result["creation_records"] = deepcopy(context["creation_records"])
    result["capture_receipt_digest"] = canonical_digest(result["creation_records"])
    with pytest.raises(ValueError, match=r"^source_bridge_result_invalid$"):
        check(result, context)


def _recorded(result, context):
    """Record the readback fingerprints of the clones as they are now."""
    capture = context["capture_facts"]
    for readback in capture["relation_readbacks"]:
        readback["metadata_digest"] = clone_fingerprint_digest(
            context["clone_metadata"][readback["destination_table"]]
        )
    result["snapshot_digest"] = canonical_digest(capture)


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_metadata_digest",
        "wrong_row_count",
        "clone_not_snapshot",
        "clone_other_base",
        "clone_outside_job",
        "clone_before_job",
        "clone_with_expiry",
        "missing_clone",
        "extra_clone",
    ],
)
def test_write_path_recomputes_each_recorded_clone_fingerprint(defect):
    result, context = fixture()
    relation = context["plan"]["snapshot_plan"]["relation_bindings"][3]
    clones = context["clone_metadata"]
    clone = clones[relation["destination_table"]]
    readback = context["capture_facts"]["relation_readbacks"][3]
    if defect == "wrong_metadata_digest":
        readback["metadata_digest"] = synthetic_digest("shape valid but not the clone")
        result["snapshot_digest"] = canonical_digest(context["capture_facts"])
    elif defect == "wrong_row_count":
        readback["row_count"] = 5
        result["snapshot_digest"] = canonical_digest(context["capture_facts"])
    else:
        if defect == "clone_not_snapshot":
            clone["type"] = "TABLE"
        elif defect == "clone_other_base":
            clone["snapshotDefinition"]["baseTableReference"]["datasetId"] = "trends_v2_dev"
        elif defect == "clone_outside_job":
            clone["creationTime"] = _millis("2026-09-21T00:30:00+00:00")
        elif defect == "clone_before_job":
            clone["creationTime"] = _millis("2026-09-21T00:20:00+00:00")
        elif defect == "clone_with_expiry":
            clone["expirationTime"] = _millis("2026-12-20T00:00:00+00:00")
        elif defect == "missing_clone":
            del clones[relation["destination_table"]]
        else:
            clones[relation["destination_table"] + "_extra"] = deepcopy(clone)
        if defect not in {"missing_clone", "extra_clone"}:
            _recorded(result, context)
    with pytest.raises(ValueError, match=r"^source_bridge_result_invalid$"):
        check(result, context)


def test_write_path_accepts_the_recorded_clones():
    result, context = fixture()
    assert check(result, context) == result


@pytest.mark.parametrize("field", ["schema", "numRows", "snapshotDefinition"])
def test_a_clone_missing_a_fingerprint_field_refuses_with_the_result_code(field):
    result, context = fixture()
    table = context["plan"]["snapshot_plan"]["relation_bindings"][3]["destination_table"]
    del context["clone_metadata"][table][field]
    with pytest.raises(ValueError, match=r"^source_bridge_result_invalid$"):
        check(result, context)
