"""Construct and compare capture proposals without granting execution authority."""

from datetime import datetime
from hashlib import sha256

from .brain_contract import canonical_bytes, canonical_digest
from .daily_execution_contracts import canonical_json_object
from .production_snapshot import _scope
from .production_snapshot_tables import PARTITIONS, _schema
from .source_estate_bridge import MARKETS, validate_bridge_profile
from .staging_source_profile import (
    GRANT_CAPTURE_POLICY_VERSION,
    validate_capture_policy_artifact,
)

PLAN_VERSION = "open_intelligence_protected_capture_plan_v3"


def build_bridge_plan(
    *, profile, source_metadata, storage_policy, cutoff_date, client_scope_id, now, artifacts
):
    policy = validate_capture_policy_artifact(storage_policy, now=now)
    checked = validate_bridge_profile(
        profile,
        cutoff_date=cutoff_date,
        observed_at=now,
        artifacts=artifacts,
        source_estate_digest=policy["grant"]["source_estate_digest"],
    )
    scope, markets = _scope(client_scope_id, MARKETS)
    if (
        storage_policy["contract_version"] != GRANT_CAPTURE_POLICY_VERSION
        or policy["grant"]["grant_id"] != checked["grant_id"]
        or policy["grant"]["source_estate_digest"] != checked["source_estate_digest"]
        or cutoff_date not in policy["allowed_cutoffs"]
    ):
        raise ValueError("source_bridge_plan_storage_mismatch")
    relations = checked["relation_bindings"]
    if (
        type(source_metadata) is not dict
        or type(source_metadata.get("tables")) is not dict
        or set(source_metadata["tables"]) != {row["source_table"] for row in relations}
    ):
        raise ValueError("source_bridge_plan_metadata_invalid")
    statements = []
    for relation in relations:
        resource = source_metadata["tables"][relation["source_table"]]
        project, dataset, table = relation["source_table"].split(".")
        partition = PARTITIONS.get(relation["lane"], "trend_date")
        if (
            type(resource) is not dict
            or resource.get("status") != 200
            or resource.get("type") != "TABLE"
            or resource.get("tableReference")
            != {"projectId": project, "datasetId": dataset, "tableId": table}
            or type(resource.get("timePartitioning")) is not dict
            or resource["timePartitioning"].get("type") != "DAY"
            or resource["timePartitioning"].get("field") != partition
        ):
            raise ValueError("source_bridge_plan_metadata_invalid")
        schema = _schema(resource.get("schema"))
        expected_type = "TIMESTAMP" if partition == "collected_at" else "DATE"
        if canonical_digest(schema) != relation["source_schema_digest"] or not any(
            field["name"] == partition and field["type"] == expected_type
            for field in schema["fields"]
        ):
            raise ValueError("source_bridge_plan_schema_mismatch")
        stamp = datetime.fromisoformat(relation["snapshot_as_of"])
        # No OPTIONS clause. A snapshot with an expiry needs bigquery.tables.deleteSnapshot on
        # the destination, and the capture identity holds only dataEditor on trends_v2_staging.
        sql = (
            f"CREATE SNAPSHOT TABLE `{relation['destination_table']}`\n"
            f"CLONE `{relation['source_table']}`\n"
            f"FOR SYSTEM_TIME AS OF TIMESTAMP '{stamp:%Y-%m-%d %H:%M:%S.%f}+00'"
        )
        statements.append(
            {
                "lane": relation["lane"],
                "sql": sql,
                "sql_digest": sha256(sql.encode("utf-8")).hexdigest(),
            }
        )
    return {
        "contract_version": PLAN_VERSION,
        "client_scope_id": scope,
        "cutoff_date": cutoff_date,
        "market_scope": list(markets),
        "snapshot_plan": checked,
        "creation_statements": statements,
    }


def validate_bridge_plan(
    value, *, profile, source_metadata, storage_policy, cutoff_date, client_scope_id, now, artifacts
):
    _, raw = canonical_json_object(value, "source_bridge_plan_invalid")
    expected = build_bridge_plan(
        profile=profile,
        source_metadata=source_metadata,
        storage_policy=storage_policy,
        cutoff_date=cutoff_date,
        client_scope_id=client_scope_id,
        now=now,
        artifacts=artifacts,
    )
    if raw != canonical_bytes(expected):
        raise ValueError("source_bridge_plan_mismatch")
    return expected
