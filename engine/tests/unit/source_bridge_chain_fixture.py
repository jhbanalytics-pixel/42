"""Test-only daily chains read through the real native chain reader.

The records are synthetic and unissued. They only let bridge tests hand the reader a chain
for a chosen operation, cutoff, payload and completion time.
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_execution_authority import (
    consume_daily_derivation,
    read_daily_execution_chain,
)

from tests.unit.test_daily_execution_authority import (
    DERIVATION_ID,
    EXECUTION,
    HEX,
    ReadClients,
    WriteClients,
    _canonical,
    _fixture,
)

STAGES = {
    "daily_source_collection": ("collection", "run"),
    "daily_source_snapshot_capture": ("capture", "initial"),
    "daily_composition_apply": ("compose", "run"),
}


class ChainClients(ReadClients):
    def __init__(self, parts, observation_json, *, times, payload, completed_at, native_at):
        super().__init__(parts, observation_json)
        self.times = times
        self.payload = payload
        self.completed_at = completed_at
        self.native_at = native_at

    def read_execution_observation(self, execution_name):
        body, storage = super().read_execution_observation(execution_name)
        return body, dict(storage, created_at=self.times["stored"])

    def read_chain(self, derivation_id, canonical_observation_json, observation_digest):
        value = super().read_chain(derivation_id, canonical_observation_json, observation_digest)
        operation = self.parts["derivation"]["operation"]
        envelope = json.loads(value["result"]["canonical_result_json"])
        envelope.update(
            authorizing_grant_digest=self.parts["derivation"]["authorizing_grant_digest"],
            operation=operation,
            operation_payload=self.payload,
            payload_contract_version=self.payload["contract_version"],
            payload_digest=hashlib.sha256(canonical_bytes(self.payload)).hexdigest(),
            completed_at=self.completed_at,
        )
        encoded = _canonical(envelope)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        result = dict(
            value["result"],
            canonical_result_json=encoded,
            result_digest=digest,
            result_id="exr_" + digest,
            operation=operation,
            completed_at=self.completed_at,
        )
        return dict(value, operation_payload=self.payload, result=result)

    def read_native_execution(self, execution_name):
        value = super().read_native_execution(execution_name)
        value.update(
            completed_at=self.native_at,
            execution_created_at=self.times["created"],
            execution_started_at=self.times["started"],
        )
        return value


def daily_chain(
    *, operation, cutoff_utc, payload, started, completed_at, native_at, grant_digest=HEX
):
    """Return (derivation_id, clients, chain) for a synthetic consumed chain."""
    _, _, _, parts = _fixture()
    start = datetime.fromisoformat(started)
    times = {
        "created": start.isoformat(),
        "started": (start + timedelta(seconds=1)).isoformat(),
        "observed": (start + timedelta(seconds=2)).isoformat(),
        "stored": (start + timedelta(seconds=3)).isoformat(),
    }
    stage, mode = STAGES[operation]
    context = parts["operation_context"]
    context.update(
        operation=operation,
        stage=stage,
        mode=mode,
        cutoff_utc=cutoff_utc,
        authorizing_grant_digest=grant_digest,
    )
    parts["derivation"]["authorizing_grant_digest"] = grant_digest
    parts["authorizing_approval"]["manifest_sha256"] = grant_digest
    if operation != "daily_source_collection":
        context.update(predecessor_result_id="exr_" + HEX, predecessor_result_digest=HEX)
    parts["derivation"]["operation"] = operation
    parts["consumption"].update(operation=operation, consumed_at=times["stored"])
    parts["grant"]["allowed_operations"] = [operation]
    observation = {
        "authorizing_grant_digest": grant_digest,
        "child_image_uri": context["child_image_uri"],
        "child_job_policy_digest": HEX,
        "child_job_resource": context["child_job_resource"],
        "child_service_identity": context["child_service_identity"],
        "contract_version": "daily_native_execution_observation_v1",
        "derivation_id": DERIVATION_ID,
        "execution_created_at": times["created"],
        "execution_name": EXECUTION,
        "execution_started_at": times["started"],
        "observed_at": times["observed"],
        "observer_principal": context["child_service_identity"],
    }
    clients = ChainClients(
        parts,
        _canonical(observation),
        times=times,
        payload=payload,
        completed_at=completed_at,
        native_at=native_at,
    )
    chain = read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)
    return DERIVATION_ID, clients, chain


def result_ref(chain):
    return {
        name: chain.result[name]
        for name in (
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }


def capture_consumption(operation="daily_source_snapshot_capture", **terms):
    """Return an issued consumption for the operation, read through the real adapter.

    ``terms`` may set the capture's ``cutoff_utc``, its grant's ``grant_id`` and the source
    policy artifact whose digest the grant carries as the estate digest.
    """
    context_json, artifact_json, observation_json, parts = _fixture()
    if operation == "daily_source_snapshot_capture":
        context_json, artifact_json = _capture_context(parts, artifact_json, **terms)
    return consume_daily_derivation(
        derivation_id=DERIVATION_ID,
        execution_name=EXECUTION,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=artifact_json,
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=hashlib.sha256(observation_json.encode()).hexdigest(),
        clients=WriteClients(parts),
    )


def _capture_context(parts, artifact_json, *, cutoff_utc=None, grant_id=None, source_policy=None):
    context = parts["operation_context"]
    context.update(
        operation="daily_source_snapshot_capture",
        stage="capture",
        mode="initial",
        predecessor_result_id="exr_" + HEX,
        predecessor_result_digest=HEX,
    )
    if cutoff_utc is not None:
        context["cutoff_utc"] = cutoff_utc
    if grant_id is not None:
        parts["grant"]["grant_id"] = grant_id
    if source_policy is not None:
        artifact_json = _canonical(
            {
                "artifacts": [
                    {
                        "data": base64.b64encode(source_policy).decode("ascii"),
                        "encoding": "base64",
                        "name": "source_policy",
                    }
                ],
                "contract_version": "daily_operation_artifact_set_v1",
            }
        )
        digest = hashlib.sha256(source_policy).hexdigest()
        context["operation_artifact_set_sha256"] = hashlib.sha256(
            artifact_json.encode()
        ).hexdigest()
        parts["grant"]["source_policy_digest"] = digest
        parts["manifest"]["input_artifacts"][1]["sha256"] = digest
    context_json = _canonical(context)
    derivation = parts["derivation"]
    derivation.update(
        operation="daily_source_snapshot_capture",
        operation_context_sha256=hashlib.sha256(context_json.encode()).hexdigest(),
        canonical_operation_context_json=context_json,
    )
    manifest = parts["manifest"]
    manifest["input_artifacts"][0]["sha256"] = derivation["operation_context_sha256"]
    manifest_json = _canonical(manifest)
    derivation.update(
        canonical_manifest_json=manifest_json,
        manifest_sha256=hashlib.sha256(manifest_json.encode()).hexdigest(),
    )
    parts["consumption"]["operation"] = "daily_source_snapshot_capture"
    parts["grant"]["allowed_operations"] = ["daily_source_snapshot_capture"]
    return context_json, artifact_json
