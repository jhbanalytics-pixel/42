from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
from src.analysis.open_intelligence.production_snapshot_tables import _creation_config
from src.analysis.open_intelligence.source_estate_bridge_creation import validate_creation_evidence
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit.test_source_estate_bridge_plan import inputs


def fixture():
    source = inputs()
    plan = build_bridge_plan(**source)
    observed = source["now"]
    earliest = observed - timedelta(minutes=20)
    client = bigquery.Client(project="ogilvy-trends-v2", credentials=AnonymousCredentials())
    evidence, jobs = [], {}
    for statement, relation in zip(
        plan["creation_statements"], plan["snapshot_plan"]["relation_bindings"], strict=True
    ):
        lane = relation["lane"]
        target = dict(
            zip(
                ("projectId", "datasetId", "tableId"),
                relation["destination_table"].split("."),
                strict=True,
            )
        )
        native = {
            "jobReference": {
                "projectId": "ogilvy-trends-v2",
                "location": "US",
                "jobId": "bridge_" + lane,
            },
            "user_email": "writer@example.iam.gserviceaccount.com",
            "configuration": _creation_config(statement["sql"]),
            "status": {"state": "DONE"},
            "statistics": {
                "creationTime": str(int(earliest.timestamp() * 1000)),
                "startTime": str(int(earliest.timestamp() * 1000)),
                "endTime": str(int((earliest + timedelta(seconds=2)).timestamp() * 1000)),
                "query": {
                    "statementType": "CREATE_SNAPSHOT_TABLE",
                    "ddlOperationPerformed": "CREATE",
                    "ddlTargetTable": target,
                    "totalBytesBilled": "11",
                },
            },
        }
        jobs[lane] = bigquery.QueryJob.from_api_repr(deepcopy(native), client)
        resource = {
            "tableReference": target,
            "location": "US",
            "type": "SNAPSHOT",
            "etag": "independent-fixture-etag",
            "creationTime": native["statistics"]["endTime"],
            "snapshotDefinition": {
                "baseTableReference": dict(
                    zip(
                        ("projectId", "datasetId", "tableId"),
                        relation["source_table"].split("."),
                        strict=True,
                    )
                ),
                "snapshotTime": relation["snapshot_as_of"],
            },
            "schema": source["source_metadata"]["tables"][relation["source_table"]]["schema"],
            "numRows": "3",
            "numBytes": "100",
        }
        evidence.append(
            {
                "lane": lane,
                "manifest_sha256": "a" * 64,
                "consumption_id": "exc_fixture",
                "execution_name": "execution_fixture",
                "native_job": native,
                "snapshot_metadata": resource,
            }
        )
    return {
        "plan": plan,
        "evidence": evidence,
        "plan_inputs": source,
        "jobs": jobs,
        "manifest_sha256": "a" * 64,
        "consumption_id": "exc_fixture",
        "execution_name": "execution_fixture",
        "writer_identity": "writer@example.iam.gserviceaccount.com",
        "earliest": earliest,
        "observed_at": observed,
        "max_bytes_billed": 100,
    }


@pytest.mark.parametrize("empty", [False, True])
def test_seven_sdk_jobs_and_role_metadata_derive_records(empty):
    args = fixture()
    if empty:
        for row in args["evidence"]:
            row["snapshot_metadata"].update(numRows="0", numBytes="0")
    result = validate_creation_evidence(**args)
    assert len(result["creation_records"]) == len(result["relation_readbacks"]) == 7
    assert result["total_bytes_billed"] == 77
    assert result["total_snapshot_bytes"] == (0 if empty else 700)
    assert {row["row_count"] for row in result["relation_readbacks"]} == ({0} if empty else {3})


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "duplicate",
        "owner",
        "sql",
        "principal",
        "running",
        "failed",
        "base",
        "snapshot_time",
        "schema",
        "count",
        "size",
        "etag",
        "expiry",
        "target",
        "billing",
        "budget",
        "end_time",
        "job_object",
    ],
)
def test_forged_or_unknown_native_evidence_refuses(defect):
    args = fixture()
    row = args["evidence"][0]
    native, meta = row["native_job"], row["snapshot_metadata"]
    if defect == "missing":
        args["evidence"].pop()
    elif defect == "duplicate":
        args["evidence"][-1] = deepcopy(row)
    elif defect == "owner":
        row["manifest_sha256"] = "b" * 64
    elif defect == "sql":
        native["configuration"]["query"]["query"] = "SELECT 1"
    elif defect == "principal":
        args["writer_identity"] = "foreign@example.com"
    elif defect == "running":
        native["status"]["state"] = "RUNNING"
    elif defect == "failed":
        native["status"]["errorResult"] = {"reason": "denied"}
    elif defect == "base":
        meta["snapshotDefinition"]["baseTableReference"]["datasetId"] = "trends_v2_dev"
    elif defect == "snapshot_time":
        meta["snapshotDefinition"]["snapshotTime"] = "2026-09-20T00:00:00Z"
    elif defect == "schema":
        meta["schema"]["fields"][1]["fields"][0]["type"] = "BYTES"
    elif defect == "count":
        meta.pop("numRows")
    elif defect == "size":
        meta["numBytes"] = "-1"
    elif defect == "etag":
        meta.pop("etag")
    elif defect == "expiry":
        meta["expirationTime"] = "0"
    elif defect == "target":
        meta["tableReference"]["tableId"] = "foreign"
    elif defect == "billing":
        native["statistics"]["query"].pop("totalBytesBilled")
    elif defect == "budget":
        args["max_bytes_billed"] = 76
    elif defect == "end_time":
        native["statistics"].pop("endTime")
    else:
        args["jobs"][row["lane"]] = object()
    with pytest.raises(ValueError):
        validate_creation_evidence(**args)


@pytest.mark.parametrize("lane", ["enriched_content", "event_ledger"])
@pytest.mark.parametrize("fraction", ["000000001", "000001000"])
def test_snapshot_time_does_not_discard_native_precision(lane, fraction):
    args = fixture()
    row = next(row for row in args["evidence"] if row["lane"] == lane)
    definition = row["snapshot_metadata"]["snapshotDefinition"]
    definition["snapshotTime"] = definition["snapshotTime"].replace("000000Z", fraction + "Z")
    with pytest.raises(ValueError):
        validate_creation_evidence(**args)


@pytest.mark.parametrize("lane", ["enriched_content", "event_ledger"])
@pytest.mark.parametrize("format_kind", ["zero_precision", "utc_offset", "local_offset"])
def test_snapshot_equivalent_native_instants_remain_accepted(lane, format_kind):
    args = fixture()
    row = next(row for row in args["evidence"] if row["lane"] == lane)
    definition = row["snapshot_metadata"]["snapshotDefinition"]
    value = definition["snapshotTime"]
    if format_kind == "zero_precision":
        value = value.replace("000000Z", "000000000Z")
    elif format_kind == "utc_offset":
        value = value.replace("Z", "+00:00")
    else:
        value = value.replace("T00:", "T02:").replace("Z", "+02:00")
    definition["snapshotTime"] = value
    assert len(validate_creation_evidence(**args)["creation_records"]) == 7


@pytest.mark.parametrize("expiry", [None, "retention", "far"])
def test_a_clone_carrying_an_expiry_is_refused(expiry):
    """The v3 statements set no expiry, so a clone that carries one is not the planned clone,
    even at its snapshot instant plus the retention days."""
    args = fixture()
    days = args["plan_inputs"]["storage_policy"]["snapshot_retention_days"]
    for row in args["evidence"]:
        meta = row["snapshot_metadata"]
        meta.pop("expirationTime", None)
        stamp = datetime.fromisoformat(meta["snapshotDefinition"]["snapshotTime"])
        if expiry == "retention":
            meta["expirationTime"] = str(int((stamp + timedelta(days=days)).timestamp() * 1000))
        elif expiry == "far":
            meta["expirationTime"] = "9999999999999"
    if expiry is None:
        assert len(validate_creation_evidence(**args)["creation_records"]) == 7
        return
    with pytest.raises(ValueError, match=r"^bridge_creation_evidence_invalid$"):
        validate_creation_evidence(**args)
