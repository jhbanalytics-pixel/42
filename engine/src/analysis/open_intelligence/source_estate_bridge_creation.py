"""Check independently supplied native creation evidence without issuing authority."""

import re
from copy import deepcopy
from datetime import UTC, datetime

from .brain_contract import canonical_digest
from .execution_approval import _format_timestamp
from .production_snapshot_tables import _creation_job, _schema, _timestamp
from .source_estate_bridge_plan import validate_bridge_plan
from .staging_source_profile import validate_capture_policy_artifact

FIELDS = frozenset(
    (
        "lane",
        "manifest_sha256",
        "consumption_id",
        "execution_name",
        "native_job",
        "snapshot_metadata",
    )
)


def _require(condition):
    if not condition:
        raise ValueError("bridge_creation_evidence_invalid")


def _integer(value):
    _require(type(value) is str and re.fullmatch(r"[0-9]+", value) is not None)
    return int(value)


def _reference(table):
    return dict(zip(("projectId", "datasetId", "tableId"), table.split("."), strict=True))


def validate_creation_evidence(
    plan,
    evidence,
    *,
    plan_inputs,
    jobs,
    manifest_sha256,
    consumption_id,
    execution_name,
    writer_identity,
    earliest,
    observed_at,
    max_bytes_billed,
):
    plan = validate_bridge_plan(plan, **plan_inputs)
    policy = validate_capture_policy_artifact(plan_inputs["storage_policy"], now=observed_at)
    constants = policy["storage_policy_constants"]
    _require(type(max_bytes_billed) is int and max_bytes_billed >= 0)
    _require(
        type(manifest_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is not None
    )
    _require(
        all(
            type(value) is str and bool(value.strip())
            for value in (consumption_id, execution_name, writer_identity)
        )
    )
    _require(
        isinstance(earliest, datetime)
        and earliest.tzinfo is not None
        and isinstance(observed_at, datetime)
        and observed_at.tzinfo is not None
        and earliest <= observed_at
    )
    relations = plan["snapshot_plan"]["relation_bindings"]
    lanes = [row["lane"] for row in relations]
    _require(
        type(evidence) is list
        and len(evidence) == len(lanes)
        and type(jobs) is dict
        and set(jobs) == set(lanes)
    )
    _require(all(type(row) is dict and set(row) == FIELDS for row in evidence))
    _require([row["lane"] for row in evidence] == lanes)
    records, readbacks, completions = [], [], {}
    billed = retained = 0
    for row, relation, statement in zip(
        evidence, relations, plan["creation_statements"], strict=True
    ):
        lane = relation["lane"]
        _require(
            row["manifest_sha256"] == manifest_sha256
            and row["consumption_id"] == consumption_id
            and row["execution_name"] == execution_name
        )
        native, resource = row["native_job"], row["snapshot_metadata"]
        _require(type(native) is dict and type(resource) is dict)
        job = jobs[lane]
        sdk_resource = deepcopy(native)
        sdk_stats = sdk_resource.get("statistics", {})
        for field in ("creationTime", "startTime", "endTime"):
            sdk_stats[field] = float(_integer(sdk_stats.get(field)))
        _require(
            canonical_digest(getattr(job, "_properties", None)) == canonical_digest(sdk_resource)
        )
        target = _reference(relation["destination_table"])
        native = _creation_job(
            job,
            native=native,
            job_id=job.job_id,
            sql=statement["sql"],
            identity=writer_identity,
            earliest=earliest,
            latest=observed_at,
            target=target,
        )
        _require(
            native["status"]["state"] == "DONE"
            and not native["status"].get("errorResult")
            and not native["status"].get("errors")
        )
        stats = native["statistics"]
        created, started, ended = (
            _integer(stats.get(key)) for key in ("creationTime", "startTime", "endTime")
        )
        _require(created <= started <= ended <= int(observed_at.timestamp() * 1000))
        billed += _integer(stats["query"].get("totalBytesBilled"))
        _require(billed <= max_bytes_billed)
        snapshot_at = _timestamp(relation["snapshot_as_of"])
        definition = resource.get("snapshotDefinition")
        _require(
            type(definition) is dict and set(definition) == {"baseTableReference", "snapshotTime"}
        )
        snapshot_text = definition["snapshotTime"]
        _require(type(snapshot_text) is str)
        native_time = re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.([0-9]+))?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            snapshot_text,
        )
        _require(native_time is not None)
        _require(not any(digit != "0" for digit in (native_time.group(1) or "")[6:]))
        _require(
            definition["baseTableReference"] == _reference(relation["source_table"])
            and _timestamp(snapshot_text) == snapshot_at
        )
        _require(
            resource.get("tableReference") == target
            and resource.get("location") == "US"
            and resource.get("type") == "SNAPSHOT"
        )
        _require(
            type(resource.get("etag")) is str
            and bool(resource["etag"].strip())
            and "streamingBuffer" not in resource
        )
        table_created = _integer(resource.get("creationTime"))
        _require(
            created <= table_created <= ended and int(snapshot_at.timestamp() * 1000) <= created
        )
        # The v3 statements set no expiry (the no-expiry ruling), so a clone carrying one is
        # not the planned clone.
        _require("expirationTime" not in resource)
        _require(
            canonical_digest(_schema(resource.get("schema"))) == relation["source_schema_digest"]
        )
        count = _integer(resource.get("numRows"))
        retained += _integer(resource.get("numBytes"))
        _require(retained <= constants["max_source_logical_bytes"])
        records.append(
            {
                **relation,
                "job_id": job.job_id,
                "native_job_digest": canonical_digest(native),
                "state": "succeeded",
            }
        )
        readbacks.append(
            {
                key: relation[key]
                for key in ("lane", "destination_table", "snapshot_as_of", "source_schema_digest")
            }
            | {"row_count": count, "metadata_digest": canonical_digest(resource)}
        )
        completions[lane] = _format_timestamp(
            datetime.fromtimestamp(ended / 1000, UTC), "bridge_creation_evidence_invalid"
        )
    return {
        "creation_records": records,
        "relation_readbacks": readbacks,
        "creation_completed_at": completions,
        "total_bytes_billed": billed,
        "total_snapshot_bytes": retained,
    }
