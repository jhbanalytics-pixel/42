"""Unissued proposal fixtures. These tests do not approve metadata, prices or grants."""

from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.production_snapshot_tables import _schema
from src.analysis.open_intelligence.source_estate_bridge_plan import (
    build_bridge_plan,
    validate_bridge_plan,
)
from src.analysis.open_intelligence.staging_source_profile import (
    GRANT_CAPTURE_POLICY_VERSION,
    build_capture_policy_artifact,
)

from tests.unit.test_source_estate_bridge_contract import (
    MAP,
    NOW,
    A,
    C,
    D,
    bridge_artifacts,
    profile,
)
from tests.unit.test_staging_source_profile import grant, measured


def inputs():
    p = profile()
    tables = {}
    for relation in p["relation_bindings"]:
        lane = relation["lane"]
        partition = (
            "collected_at"
            if lane in {"raw_content", "enriched_content"}
            else ("proposed_date" if lane == "seed_candidates" else "trend_date")
        )
        project, dataset, table = relation["source_table"].split(".")
        resource = {
            "status": 200,
            "type": "TABLE",
            "etag": "unissued-test-etag",
            "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
            "timePartitioning": {"type": "DAY", "field": partition},
            "schema": {
                "fields": [
                    {
                        "name": partition,
                        "type": "TIMESTAMP" if partition == "collected_at" else "DATE",
                        "mode": "REQUIRED",
                    },
                    {
                        "name": "fixture_payload",
                        "type": "RECORD",
                        "fields": [{"name": "value", "type": "STRING"}],
                    },
                ]
            },
        }
        tables[relation["source_table"]] = resource
        relation["source_schema_digest"] = canonical_digest(_schema(resource["schema"]))
    p["schema_digest"] = canonical_digest(
        [
            {name: relation[name] for name in ("lane", "source_table", "source_schema_digest")}
            for relation in p["relation_bindings"]
        ]
    )
    now = datetime.fromisoformat(NOW)
    storage = build_capture_policy_artifact(
        GRANT_CAPTURE_POLICY_VERSION,
        price_inputs=measured(
            observed_at=now - timedelta(hours=1),
            source_logical_bytes=7 * 4096,
            max_bytes_billed_per_query=4096,
            queries_per_cycle=7,
            jobs_per_cycle=7,
        ),
        now=now,
        grant=grant(
            grant_id=p["grant_id"],
            source_estate_digest=p["source_estate_digest"],
            valid_from=now - timedelta(days=2),
            valid_until=now + timedelta(days=2),
            allowed_cutoffs=[D],
        ),
    )
    return {
        "profile": p,
        "source_metadata": {"tables": tables},
        "storage_policy": storage,
        "cutoff_date": D,
        "client_scope_id": "ogilvy_default",
        "now": now,
        "artifacts": bridge_artifacts(),
    }


def test_builder_and_acting_validator_roundtrip_exact_seven_role_specific_statements():
    source = inputs()
    plan = build_bridge_plan(**source)
    assert set(plan) == {
        "contract_version",
        "client_scope_id",
        "cutoff_date",
        "market_scope",
        "snapshot_plan",
        "creation_statements",
    }
    assert plan["contract_version"] == "open_intelligence_protected_capture_plan_v3"
    assert plan["snapshot_plan"] == source["profile"]
    assert plan["market_scope"] == ["ke", "ng", "za"]
    assert [row["lane"] for row in plan["creation_statements"]] == sorted(row[0] for row in MAP)
    for statement, relation in zip(
        plan["creation_statements"], source["profile"]["relation_bindings"], strict=True
    ):
        stamp = datetime.fromisoformat(A if relation["role"] == "collection_evidence" else C)
        # No OPTIONS clause: a snapshot with an expiry needs bigquery.tables.deleteSnapshot on
        # the destination, which the capture identity does not hold on trends_v2_staging.
        expected = f"CREATE SNAPSHOT TABLE `{relation['destination_table']}`\nCLONE `{relation['source_table']}`\nFOR SYSTEM_TIME AS OF TIMESTAMP '{stamp:%Y-%m-%d %H:%M:%S.%f}+00'"
        assert set(statement) == {"lane", "sql", "sql_digest"}
        assert statement["sql"] == expected
        assert statement["sql_digest"] == sha256(expected.encode()).hexdigest()
    assert validate_bridge_plan(canonical_bytes(plan), **source) == plan
    plan["creation_statements"].clear()
    assert len(build_bridge_plan(**source)["creation_statements"]) == 7


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "extra",
        "legacy_keys",
        "source_identity",
        "not_table",
        "partition",
        "nested_schema",
        "missing_schema",
        "serialized_resource",
        "serialized_schema",
    ],
)
def test_full_source_metadata_is_checked_by_exact_fully_qualified_relation(defect):
    source = inputs()
    tables = source["source_metadata"]["tables"]
    key = next(iter(tables))
    resource = tables[key]
    if defect == "missing":
        del tables[key]
    elif defect == "extra":
        tables["ogilvy-trends-v2.trends_v2_dev.extra"] = deepcopy(resource)
    elif defect == "legacy_keys":
        source["source_metadata"]["tables"] = {k.split(".", 1)[1]: v for k, v in tables.items()}
    elif defect == "source_identity":
        resource["tableReference"]["datasetId"] = "trends_v2_dev"
    elif defect == "not_table":
        resource["type"] = "VIEW"
    elif defect == "partition":
        resource["timePartitioning"]["field"] = "published_at"
    elif defect == "nested_schema":
        resource["schema"]["fields"][1]["fields"][0]["type"] = "BYTES"
    elif defect == "missing_schema":
        del resource["schema"]
    elif defect == "serialized_resource":
        tables[key] = canonical_bytes(resource).decode()
    else:
        resource["schema"] = canonical_bytes(resource["schema"]).decode()
    with pytest.raises(ValueError):
        build_bridge_plan(**source)


@pytest.mark.parametrize(
    "defect",
    [
        "version",
        "extra",
        "five_records",
        "duplicate",
        "order",
        "arbitrary_sql",
        "sql_digest",
        "role",
        "source",
        "destination",
        "instant",
        "schema",
        "serialized_statement",
        "client_scope",
        "market_scope",
    ],
)
def test_acting_validation_regenerates_the_plan_instead_of_trusting_its_hashes(defect):
    source = inputs()
    plan = build_bridge_plan(**source)
    rows = plan["creation_statements"]
    relation = plan["snapshot_plan"]["relation_bindings"][0]
    if defect == "version":
        plan["contract_version"] = "open_intelligence_protected_capture_plan_v2"
    elif defect == "extra":
        plan["approved"] = True
    elif defect == "five_records":
        plan["creation_statements"] = rows[:5]
    elif defect == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif defect == "order":
        rows.reverse()
    elif defect == "arbitrary_sql":
        rows[0]["sql"] = "SELECT 1"
        rows[0]["sql_digest"] = sha256(b"SELECT 1").hexdigest()
    elif defect == "sql_digest":
        rows[0]["sql_digest"] = "0" * 64
    elif defect == "role":
        relation["role"] = "derived_history"
    elif defect == "source":
        relation["source_table"] = "ogilvy-trends-v2.trends_v2_dev.enriched_content"
    elif defect == "destination":
        relation["destination_table"] += "_alternate"
    elif defect == "instant":
        relation["snapshot_as_of"] = C
    elif defect == "schema":
        relation["source_schema_digest"] = "0" * 64
    elif defect == "serialized_statement":
        rows[0] = canonical_bytes(rows[0]).decode()
    elif defect == "client_scope":
        plan["client_scope_id"] = "another_client"
    else:
        plan["market_scope"] = ["za"]
    with pytest.raises(ValueError):
        validate_bridge_plan(plan, **source)


@pytest.mark.parametrize("defect", ["retention", "expired_price", "grant", "estate", "cutoff"])
def test_storage_policy_is_validated_and_bound_without_loading_authority(defect):
    source = inputs()
    policy = source["storage_policy"]
    if defect == "retention":
        policy["snapshot_retention_days"] = 1
    elif defect == "expired_price":
        policy["price_review"]["expires_at"] = (source["now"] - timedelta(minutes=1)).isoformat()
    elif defect == "grant":
        source["profile"]["grant_id"] = "different_valid_grant"
    elif defect == "estate":
        source["profile"]["source_estate_digest"] = "0" * 64
    else:
        policy["grant"]["allowed_cutoffs"] = ["2026-09-19"]
    with pytest.raises(ValueError):
        build_bridge_plan(**source)


@pytest.mark.parametrize(
    "artifact", ["temporal_rules", "collection_receipt_set", "history_completion_set"]
)
def test_plan_refuses_artifacts_its_profile_does_not_name(artifact):
    source = inputs()
    changed = source["artifacts"][artifact]
    if artifact == "temporal_rules":
        changed["rules"][1]["endpoint"] = "unreviewed"
    elif artifact == "collection_receipt_set":
        changed["receipts"][0]["collection_receipt_digest"] = "0" * 64
    else:
        changed["entries"][-1]["reason_code"] = "substituted"
    with pytest.raises(ValueError, match=r"^source_bridge_artifact_digest_differs$"):
        build_bridge_plan(**source)
