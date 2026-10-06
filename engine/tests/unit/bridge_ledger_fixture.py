"""Test-only approval-ledger bridge captures under a synthetic, unissued generation.

The generation is the active origin registry with its source snapshot capture policy
rewritten to take the v3 bridge plan and its nine input artifacts. It lives in a temporary
directory, is never trusted by the packaged catalogue and grants nothing. The approval,
consumption and result are typed through the real v2 record classes under it.
"""

import hashlib
import json
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

from tests.unit.test_execution_manifest_origins import load_registry, manifest
from tests.unit.test_execution_records_v2 import APPROVED_BY, RESOURCE_SHA, context
from tests.unit.test_retained_readers_v2 import FakeCatalogue, v2_rows

ENGINE_ROOT = Path(__file__).resolve().parents[2]
POLICY = "configs/open_intelligence/origin_contracts/successor-source-snapshot-capture.json"
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
IDENTITY = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
APPROVED_AT = datetime(2026, 9, 21, 0, 21, tzinfo=UTC)
CONSUMED_AT = datetime(2026, 9, 21, 0, 22, tzinfo=UTC)
JOB_STARTED = datetime(2026, 9, 21, 0, 23, tzinfo=UTC)
JOB_ENDED = datetime(2026, 9, 21, 0, 24, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 9, 21, 0, 26, 50, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


RULE_ARGUMENTS = {
    "plan_contract_version": "open_intelligence_protected_capture_plan_v3",
    "consume_routine": "sp_consume_open_intelligence_source_snapshot_v3",
}


def bridge_registry(tmp_path, rule_arguments=None):
    """The active origin registry, copied, with the capture policy taking the v3 plan.

    ``rule_arguments`` replaces some of the rewritten rule's arguments, so a generation
    whose capture rule is not the v3 bridge rule can be built the same way.
    """
    root = tmp_path / "generation"
    contracts = root / "configs/open_intelligence/origin_contracts"
    contracts.mkdir(parents=True)
    for item in (ENGINE_ROOT / "configs/open_intelligence/origin_contracts").iterdir():
        shutil.copy(item, contracts / item.name)
    policy = json.loads((root / POLICY).read_bytes())
    rule = policy["operation_validation"]["source_snapshot_capture"]
    rule["input_artifact_names"] = list(LEDGER_ARTIFACTS)
    rule["arguments"].update(RULE_ARGUMENTS, **(rule_arguments or {}))
    raw = canonical_bytes(policy)
    (root / POLICY).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    registry = json.loads(
        (ENGINE_ROOT / "configs/open_intelligence/execution_origins_v1.json").read_bytes()
    )
    for row in registry["rows"]:
        if row["contract_file"] == POLICY:
            row["contract_sha256"] = digest
            row["contract_digests"] = [digest]
    path = root / "configs/open_intelligence/execution_origins_bridge_test.json"
    path.write_bytes(canonical_bytes(registry))
    return load_registry(path, root=root), digest


def ledger_manifest(contract_sha256, raw_artifacts, *, cutoff, grant_id, mode="initial"):
    value = manifest("source_snapshot_capture")
    value["contract_sha256"] = contract_sha256
    # The switches are the capture policy's own, read from the active origin contract.
    rule = json.loads((ENGINE_ROOT / POLICY).read_bytes())["operation_validation"]
    switches = rule["source_snapshot_capture"]["arguments"]
    value["arguments"] = [
        *switches["prefix"],
        cutoff,
        switches["mode_switch"],
        mode,
        switches["grant_switch"],
        grant_id,
    ]
    value["input_artifacts"] = [
        {"name": name, "sha256": hashlib.sha256(raw_artifacts[name]).hexdigest()}
        for name in LEDGER_ARTIFACTS
    ]
    value["timeout_seconds"] = 3600
    value["expires_at"] = EXPIRES_AT.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def manifest_sha256(registry, manifest_value):
    """The digest of the canonical manifest an approval of ``manifest_value`` records."""
    return hashlib.sha256(
        execution_approval.canonical_manifest_bytes(
            manifest_value, mode="new_consume", registry=registry
        )
    ).hexdigest()


def initial_job_id(manifest_sha, lane):
    """The creation job id an initial capture derives from its approved manifest."""
    return f"oi_v3_snapshot_{manifest_sha}_{lane}"


def ledger_capture(
    registry,
    manifest_value,
    payload,
    *,
    approved_at=APPROVED_AT,
    consumed_at=CONSUMED_AT,
    completed_at=COMPLETED_AT,
    status="succeeded",
    result_reference=None,
    operation="source_snapshot_capture",
    execution_id="bridge-capture",
    reference_suffix="#source-snapshot",
    expires_at=EXPIRES_AT,
):
    """Type an approval, consumption and result through the real v2 record classes.

    The chain is a source snapshot capture's unless ``operation`` names another operation
    of the manifest, whose execution ``execution_id`` names and whose result reference is
    that execution's name with ``reference_suffix``.
    """
    canonical_manifest = execution_approval.canonical_manifest_bytes(
        manifest_value, mode="new_consume", registry=registry
    )
    manifest_sha = hashlib.sha256(canonical_manifest).hexdigest()
    approval_id = execution_approval.approval_id_v2(
        manifest_sha,
        APPROVED_BY,
        approved_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    approval = execution_approval.ExecutionApprovalV2(
        approval_contract_version="open_intelligence_execution_approval_v2",
        approval_id=approval_id,
        manifest_version="open_intelligence_execution_manifest_v2",
        operation=operation,
        contract_sha256=manifest_value["contract_sha256"],
        manifest_sha256=manifest_sha,
        canonical_manifest_json=canonical_manifest.decode(),
        approved_by=APPROVED_BY,
        approved_at=approved_at,
        expires_at=expires_at,
        approval_phrase_sha256=execution_approval._approval_phrase_sha256_v2(
            operation, manifest_sha, registry.sha256, RESOURCE_SHA
        ),
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    execution_name = manifest_value["job_resource"] + "/executions/" + execution_id
    consumption_id = execution_approval.consumption_id_v2(
        approval_id,
        execution_name,
        consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
    )
    consumption = execution_approval.ExecutionConsumptionV2(
        consumption_contract_version="open_intelligence_execution_consumption_v2",
        consumption_id=consumption_id,
        approval_id=approval_id,
        manifest_sha256=manifest_sha,
        operation=operation,
        execution_name=execution_name,
        job_resource=manifest_value["job_resource"],
        source_sha=manifest_value["source_sha"],
        image_uri=manifest_value["image_uri"],
        consumed_at=consumed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    raw = canonical_bytes(payload).decode()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    reference = result_reference or execution_name + reference_suffix
    result = execution_approval.ExecutionResultV2(
        result_contract_version="open_intelligence_execution_result_v2",
        result_id=execution_approval.result_id_v2(
            consumption_id,
            reference,
            digest,
            status,
            completed_at,
            origin_registry_sha256=registry.sha256,
            resource_manifest_sha256=RESOURCE_SHA,
        ),
        consumption_id=consumption_id,
        approval_id=approval_id,
        manifest_sha256=manifest_sha,
        operation=operation,
        execution_name=execution_name,
        result_reference=reference,
        canonical_result_json=raw,
        result_digest=digest,
        status=status,
        completed_at=completed_at,
        origin_registry_sha256=registry.sha256,
        resource_manifest_sha256=RESOURCE_SHA,
        **context(registry),
    )
    return {
        "approval": approval,
        "consumption": consumption,
        "result": result,
        "rows": v2_rows(approval, consumption, result),
        "catalogue": FakeCatalogue(registry, (registry.sha256, RESOURCE_SHA)),
    }


def millis(instant):
    return str(int(instant.timestamp() * 1000))


def native_job(statement, relation, job_id, *, identity=IDENTITY, started=None, ended=None):
    """A finished CREATE SNAPSHOT TABLE job resource as jobs.get returns it."""
    project, dataset, table = relation["destination_table"].split(".")
    return {
        "jobReference": {"projectId": "ogilvy-trends-v2", "location": "US", "jobId": job_id},
        "user_email": identity,
        "configuration": {"query": {"query": statement["sql"]}},
        "status": {"state": "DONE"},
        "statistics": {
            "creationTime": millis(started or JOB_STARTED),
            "startTime": millis(started or JOB_STARTED),
            "endTime": millis(ended or JOB_ENDED),
            "query": {
                "statementType": "CREATE_SNAPSHOT_TABLE",
                "ddlOperationPerformed": "CREATE",
                "ddlTargetTable": {"projectId": project, "datasetId": dataset, "tableId": table},
            },
        },
    }


def creation(plan, *, job_id, **job_changes):
    """Native jobs for each plan statement and the creation records that name them.

    ``job_id(lane)`` names each lane's creation job.
    """
    jobs, records = {}, []
    relations = plan["snapshot_plan"]["relation_bindings"]
    for relation, statement in zip(relations, plan["creation_statements"], strict=True):
        name = job_id(relation["lane"])
        job = native_job(statement, relation, name, **job_changes)
        jobs[name] = job
        records.append(
            {
                **deepcopy(relation),
                "job_id": name,
                "native_job_digest": canonical_digest(job),
                "state": "succeeded",
            }
        )
    return jobs, records


def clone_readback(plan):
    """The stored capture facts and the tables.get resource of each clone, synthetic.

    Each clone is created inside its creating job's window, and the facts record each
    clone's content fingerprint as the capture read it back.
    """
    from tests.unit.test_source_estate_bridge_contract import (
        clone_fingerprint_digest,
        clone_resource,
        facts,
    )

    profile = plan["snapshot_plan"]
    created = millis(JOB_STARTED.replace(second=30))
    clones = {
        relation["destination_table"]: clone_resource(
            relation, creationTime=created, lastModifiedTime=created
        )
        for relation in profile["relation_bindings"]
    }
    value = facts(profile)
    for readback in value["relation_readbacks"]:
        readback["metadata_digest"] = clone_fingerprint_digest(
            clones[readback["destination_table"]]
        )
    return {"capture_facts": value, "clone_metadata": clones}
