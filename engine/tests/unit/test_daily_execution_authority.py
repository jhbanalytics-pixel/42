import base64
import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import STEP_NUMBERS_V2
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityBudget,
    DailyAuthorityIntegrityError,
    DailyAuthorityUnavailable,
    NativeDailyAuthorityReadAdapter,
    NativeDailyAuthorityWriteAdapter,
    cancel_daily_derivation,
    consume_daily_derivation,
    input_artifact_sha256,
    native_instant_key,
    read_daily_execution_chain,
    record_daily_result,
    require_daily_consumption,
)
from src.analysis.open_intelligence.daily_execution_contracts import validate_capture_semantics

HEX = "a" * 64
DERIVATION_ID = "exd_" + "b" * 64
EXECUTION = "projects/p/locations/us/jobs/daily/executions/run-1"
CONSUMED_AT = "2026-09-14T00:01:03+00:00"
ORIGIN = "d" * 64
RESOURCE = "e" * 64


def _canonical(value):
    return canonical_bytes(value).decode("utf-8")


def _fixture():
    artifact = canonical_bytes({"policy": "bounded"})
    artifact_set = {
        "artifacts": [
            {
                "data": base64.b64encode(artifact).decode("ascii"),
                "encoding": "base64",
                "name": "source_policy",
            }
        ],
        "contract_version": "daily_operation_artifact_set_v1",
    }
    artifact_json = _canonical(artifact_set)
    context = {
        "attempt_version": 1,
        "authorizing_approval_id": "exa_" + HEX,
        "authorizing_grant_digest": HEX,
        "business_attempt_id": "bat_" + HEX,
        "child_image_uri": "repo/image@sha256:" + HEX,
        "child_job_policy_digest": HEX,
        "child_job_resource": "projects/p/locations/us/jobs/daily",
        "child_service_identity": "service@example.iam.gserviceaccount.com",
        "contract_version": "daily_operation_context_v1",
        "cutoff_utc": "2026-09-14T00:00:00+00:00",
        "execution_observation_prefix": "42/daily/execution-observations/",
        "gcs_control_object": "42/daily/slots/x/control.json",
        "input_digest": HEX,
        "intent_id": "dsi_" + HEX,
        "lease_epoch": 1,
        "lease_owner": "projects/p/locations/us/jobs/parent/executions/p1",
        "mode": "run",
        "operation": "daily_source_collection",
        "operation_artifact_set_sha256": hashlib.sha256(artifact_json.encode()).hexdigest(),
        "parent_execution_name": "projects/p/locations/us/jobs/parent/executions/p1",
        "parent_job_resource": "projects/p/locations/us/jobs/parent",
        "parent_principal": "parent@example.iam.gserviceaccount.com",
        "predecessor_result_digest": None,
        "predecessor_result_id": None,
        "profile_digest": HEX,
        "slot_id": HEX,
        "source_estate_id": "intelligence-42-core",
        "stage": "collection",
    }
    context_json = _canonical(context)
    observation = {
        "authorizing_grant_digest": HEX,
        "child_image_uri": context["child_image_uri"],
        "child_job_policy_digest": HEX,
        "child_job_resource": context["child_job_resource"],
        "child_service_identity": context["child_service_identity"],
        "contract_version": "daily_native_execution_observation_v1",
        "derivation_id": DERIVATION_ID,
        "execution_created_at": "2026-09-14T00:01:00+00:00",
        "execution_name": EXECUTION,
        "execution_started_at": "2026-09-14T00:01:01+00:00",
        "observed_at": "2026-09-14T00:01:02+00:00",
        "observer_principal": context["child_service_identity"],
    }
    observation_json = _canonical(observation)
    derivation = {
        "authorizing_approval_id": context["authorizing_approval_id"],
        "authorizing_grant_digest": HEX,
        "business_attempt_id": context["business_attempt_id"],
        "child_job_resource": context["child_job_resource"],
        "derivation_id": DERIVATION_ID,
        "manifest_sha256": HEX,
        "operation": "daily_source_collection",
        "operation_context_sha256": hashlib.sha256(context_json.encode()).hexdigest(),
    }
    consumption = {
        "approval_id": DERIVATION_ID,
        "execution_name": EXECUTION,
        "operation": "daily_source_collection",
    }
    manifest = {
        "input_artifacts": [
            {"name": "operation_context", "sha256": derivation["operation_context_sha256"]},
            {"name": "source_policy", "sha256": hashlib.sha256(artifact).hexdigest()},
        ]
    }
    manifest_json = _canonical(manifest)
    derivation["canonical_manifest_json"] = manifest_json
    derivation["canonical_operation_context_json"] = context_json
    derivation["manifest_sha256"] = hashlib.sha256(manifest_json.encode()).hexdigest()
    consumption_id = (
        "exc_"
        + hashlib.sha256(
            canonical_bytes(
                {
                    "consumed_at": CONSUMED_AT,
                    "consumption_contract_version": "open_intelligence_execution_consumption_v3",
                    "derivation_id": DERIVATION_ID,
                    "execution_name": EXECUTION,
                    "execution_observation_sha256": hashlib.sha256(
                        observation_json.encode()
                    ).hexdigest(),
                    "origin_registry_sha256": ORIGIN,
                    "resource_manifest_sha256": RESOURCE,
                }
            )
        ).hexdigest()
    )
    consumption["consumed_at"] = CONSUMED_AT
    consumption["consumption_contract_version"] = "open_intelligence_execution_consumption_v3"
    consumption["consumption_id"] = consumption_id
    consumption["origin_registry_sha256"] = ORIGIN
    consumption["resource_manifest_sha256"] = RESOURCE
    parts = {
        "authorizing_approval": {
            "approval_id": context["authorizing_approval_id"],
            "manifest_sha256": HEX,
        },
        "consumption": consumption,
        "derivation": derivation,
        "grant": {
            "allowed_operations": ["daily_source_collection"],
            "contract_version": "42_recurring_execution_grant_v2",
            "grant_id": "grant-1",
            "source_policy_digest": hashlib.sha256(artifact).hexdigest(),
        },
        "manifest": manifest,
        "operation_context": context,
    }
    return context_json, artifact_json, observation_json, parts


class WriteClients:
    def __init__(self, parts):
        self.parts = parts
        self.calls = []

    def consume_derivation(self, *args):
        self.calls.append(args)
        return self.parts

    def read_native_job(self, job_resource):
        self.calls.append(("job", job_resource))
        context = self.parts["operation_context"]
        return {
            "grant_digest": HEX,
            "image_uri": context["child_image_uri"],
            "job_policy_digest": context["child_job_policy_digest"],
            "job_resource": job_resource,
            "service_identity": context["child_service_identity"],
        }

    def read_native_execution(self, execution_name):
        self.calls.append(("execution", execution_name))
        value = dict(
            self.read_native_job(self.parts["operation_context"]["child_job_resource"]),
            execution_name=execution_name,
        )
        value.update(
            execution_created_at="2026-09-14T00:01:00+00:00",
            execution_started_at="2026-09-14T00:01:01+00:00",
            step_number=STEP_NUMBERS_V2[self.parts["operation_context"]["stage"]],
        )
        return value

    def admit_execution_observation(self, execution_name, body, digest):
        self.calls.append(("observation", execution_name))
        return body, {
            "content_sha256": digest,
            "created_at": "2026-09-14T00:01:03+00:00",
            "generation": "1",
            "object_name": "42/daily/execution-observations/"
            + hashlib.sha256(execution_name.encode()).hexdigest()
            + ".json",
            "size_bytes": len(body),
        }

    def read_provider_operation(self, operation_name):
        return {
            "execution_created": False,
            "observed_at": "2026-09-14T00:03:00+00:00",
            "observer_principal": "parent@example.iam.gserviceaccount.com",
            "observation_reference": "provider-operations/op-1",
            "operation_name": operation_name,
            "terminal_state": "done_no_execution",
        }


class ReadClients:
    def __init__(self, parts, observation_json):
        self.parts = parts
        self.observation = observation_json.encode()

    def read_derivation(self, derivation_id):
        return {
            "consumption": self.parts["consumption"],
            "derivation": self.parts["derivation"],
            "lifecycle_state": "consumed",
            "tombstone": None,
        }

    def read_execution_observation(self, execution_name):
        return self.observation, {
            "content_sha256": hashlib.sha256(self.observation).hexdigest(),
            "created_at": "2026-09-14T00:01:03+00:00",
            "generation": "1",
            "object_name": "42/daily/execution-observations/"
            + hashlib.sha256(execution_name.encode()).hexdigest()
            + ".json",
            "size_bytes": len(self.observation),
        }

    def read_chain(self, derivation_id, canonical_observation_json, observation_digest):
        payload = {"contract_version": "payload_v1", "value": "measured"}
        envelope = {
            "authorizing_approval_id": self.parts["derivation"]["authorizing_approval_id"],
            "authorizing_grant_digest": HEX,
            "business_attempt_id": "bat_" + HEX,
            "child_job_resource": self.parts["operation_context"]["child_job_resource"],
            "completed_at": "2026-09-14T00:02:00+00:00",
            "consumption_id": self.parts["consumption"]["consumption_id"],
            "contract_version": "daily_execution_result_v1",
            "derivation_id": DERIVATION_ID,
            "effect_state": "effects_recorded",
            "execution_name": EXECUTION,
            "execution_observation_sha256": observation_digest,
            "manifest_sha256": self.parts["derivation"]["manifest_sha256"],
            "operation": "daily_source_collection",
            "operation_context_sha256": self.parts["derivation"]["operation_context_sha256"],
            "operation_payload": payload,
            "payload_contract_version": "payload_v1",
            "payload_digest": hashlib.sha256(canonical_bytes(payload)).hexdigest(),
            "result_reference": "gs://protected/result.json",
            "spend_state": "measured",
            "stage_metering": {
                "complete": True,
                "query_count": 1,
                "total_bytes_billed": 0,
                "storage_write_count": 1,
                "storage_write_bytes": 1,
                "vendor_credits": "0",
                "model_calls": 0,
            },
            "terminal_state": "succeeded",
        }
        encoded = _canonical(envelope)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        result = {
            "approval_id": DERIVATION_ID,
            "canonical_result_json": encoded,
            "consumption_id": self.parts["consumption"]["consumption_id"],
            "execution_name": EXECUTION,
            "manifest_sha256": self.parts["derivation"]["manifest_sha256"],
            "operation": "daily_source_collection",
            "result_reference": "gs://protected/result.json",
            "result_digest": digest,
            "result_id": "exr_" + digest,
            "result_contract_version": "open_intelligence_execution_result_v3",
            "completed_at": envelope["completed_at"],
            "origin_registry_sha256": ORIGIN,
            "resource_manifest_sha256": RESOURCE,
            "status": "succeeded",
        }
        return dict(self.parts, operation_payload=payload, result=result)

    def read_native_execution(self, execution_name):
        context = self.parts["operation_context"]
        return {
            "completed_at": "2026-09-14T00:01:59+00:00",
            "execution_created_at": "2026-09-14T00:01:00+00:00",
            "execution_name": execution_name,
            "execution_started_at": "2026-09-14T00:01:01+00:00",
            "image_uri": context["child_image_uri"],
            "job_policy_digest": context["child_job_policy_digest"],
            "job_resource": context["child_job_resource"],
            "service_identity": context["child_service_identity"],
            "terminal_state": "succeeded",
        }


def test_real_consume_adapter_issues_sealed_immutable_capability():
    context, artifacts, observation, parts = _fixture()
    clients = WriteClients(parts)
    admitted = consume_daily_derivation(
        derivation_id=DERIVATION_ID,
        execution_name=EXECUTION,
        canonical_operation_context_json=context,
        canonical_operation_artifact_set_json=artifacts,
        canonical_execution_observation_json=observation,
        execution_observation_sha256=hashlib.sha256(observation.encode()).hexdigest(),
        clients=clients,
    )
    assert require_daily_consumption(admitted, operation="daily_source_collection") is admitted
    assert (
        admitted.input_artifact_sha256("source_policy")
        == parts["manifest"]["input_artifacts"][1]["sha256"]
    )
    assert (
        input_artifact_sha256(admitted, "source_policy")
        == parts["manifest"]["input_artifacts"][1]["sha256"]
    )
    with pytest.raises(DailyAuthorityIntegrityError, match="immutable"):
        admitted.operation = "daily_certification"
    with pytest.raises(DailyAuthorityIntegrityError, match="copy_refused"):
        copy.copy(admitted)
    with pytest.raises(DailyAuthorityIntegrityError, match="capability_invalid"):
        require_daily_consumption({}, operation="daily_source_collection")
    assert [call[0] if isinstance(call, tuple) else None for call in clients.calls[:4]] == [
        "job",
        "execution",
        "job",
        "observation",
    ]
    assert len(clients.calls) == 5


def test_artifact_mutation_refuses_before_consume_transport():
    context, artifacts, observation, parts = _fixture()
    changed = json.loads(artifacts)
    changed["artifacts"][0]["data"] = base64.b64encode(b"changed").decode()
    clients = WriteClients(parts)
    with pytest.raises(DailyAuthorityIntegrityError, match="artifact_set_digest_mismatch"):
        consume_daily_derivation(
            derivation_id=DERIVATION_ID,
            execution_name=EXECUTION,
            canonical_operation_context_json=context,
            canonical_operation_artifact_set_json=_canonical(changed),
            canonical_execution_observation_json=observation,
            execution_observation_sha256=hashlib.sha256(observation.encode()).hexdigest(),
            clients=clients,
        )
    assert clients.calls == []


def test_native_execution_mismatch_refuses_before_sql_consumption():
    context, artifacts, observation, parts = _fixture()
    clients = WriteClients(parts)
    clients.read_native_execution = lambda name: {
        "execution_name": name,
        "grant_digest": HEX,
        "image_uri": "repo/other@sha256:" + HEX,
        "job_policy_digest": HEX,
        "job_resource": parts["operation_context"]["child_job_resource"],
        "service_identity": parts["operation_context"]["child_service_identity"],
    }
    with pytest.raises(DailyAuthorityIntegrityError, match="daily_native_execution_mismatch"):
        consume_daily_derivation(
            derivation_id=DERIVATION_ID,
            execution_name=EXECUTION,
            canonical_operation_context_json=context,
            canonical_operation_artifact_set_json=artifacts,
            canonical_execution_observation_json=observation,
            execution_observation_sha256=hashlib.sha256(observation.encode()).hexdigest(),
            clients=clients,
        )
    assert not any(isinstance(call, tuple) and len(call) == 6 for call in clients.calls)


def test_substituted_protected_context_refuses_capability():
    context, artifacts, observation, parts = _fixture()
    substituted = copy.deepcopy(parts)
    substituted["operation_context"]["business_attempt_id"] = "bat_" + "9" * 64
    clients = WriteClients(substituted)
    with pytest.raises(DailyAuthorityIntegrityError, match="daily_protected_readback_mismatch"):
        consume_daily_derivation(
            derivation_id=DERIVATION_ID,
            execution_name=EXECUTION,
            canonical_operation_context_json=context,
            canonical_operation_artifact_set_json=artifacts,
            canonical_execution_observation_json=observation,
            execution_observation_sha256=hashlib.sha256(observation.encode()).hexdigest(),
            clients=clients,
        )


def test_cancellation_rejects_empty_or_unproved_provider_reconciliation():
    _context, _artifacts, _observation, parts = _fixture()
    clients = WriteClients(parts)
    empty = _canonical({})
    with pytest.raises(ValueError, match="daily_reconciliation_invalid"):
        cancel_daily_derivation(
            derivation_id=DERIVATION_ID,
            reason_code="provider_terminal_no_execution",
            canonical_reconciliation_json=empty,
            reconciliation_digest=hashlib.sha256(empty.encode()).hexdigest(),
            clients=clients,
        )
    reconciliation = {
        "contract_version": "daily_dispatch_reconciliation_v1",
        "derivation_id": DERIVATION_ID,
        "dispatch_attempted": True,
        "dispatch_observation_reference": "provider-operations/op-1",
        "execution_created": False,
        "observed_at": "2026-09-14T00:03:00+00:00",
        "observer_principal": "parent@example.iam.gserviceaccount.com",
        "provider_operation_name": "operations/op-1",
        "provider_terminal_state": "done_no_execution",
        "reason_code": "provider_terminal_no_execution",
    }
    encoded = _canonical(reconciliation)
    clients.read_provider_operation = lambda _name: {
        "operation_name": "operations/op-1",
        "terminal_state": "running",
    }
    with pytest.raises(DailyAuthorityIntegrityError, match="daily_provider_operation_mismatch"):
        cancel_daily_derivation(
            derivation_id=DERIVATION_ID,
            reason_code="provider_terminal_no_execution",
            canonical_reconciliation_json=encoded,
            reconciliation_digest=hashlib.sha256(encoded.encode()).hexdigest(),
            clients=clients,
        )


def test_capture_semantics_reject_wrong_cutoff_estate_and_recovery_chain():
    context = {
        "cutoff_utc": "2026-09-14T00:00:00+00:00",
        "mode": "initial",
        "predecessor_result_digest": None,
        "predecessor_result_id": None,
    }
    plan = {
        "contract_version": "open_intelligence_protected_capture_plan_v2",
        "cutoff_date": "2026-09-13",
        "snapshot_plan": {
            "observation_window_end": "2026-09-14T00:00:00+00:00",
            "profile_version": "42_staging_source_v2",
            "projection_version": "native_id_bound_v1",
            "source_dataset": "intelligence_42_sources_staging",
            "source_estate_digest": HEX,
        },
    }
    storage = {
        "contract_version": "open_intelligence_source_capture_storage_v2",
        "grant": {"source_estate_digest": HEX},
    }
    artifacts = {
        name: canonical_bytes({"contract_version": name + "_v1"})
        for name in (
            "build_provenance",
            "capture_contract",
            "cost_policy",
            "daily_profile",
            "source_metadata",
        )
    }
    artifacts.update(
        capture_plan=canonical_bytes(plan),
        storage_policy=canonical_bytes(storage),
        recovery_context=b"null",
    )
    validate_capture_semantics(context, artifacts)
    changed = dict(artifacts, capture_plan=canonical_bytes(dict(plan, cutoff_date="2026-09-12")))
    with pytest.raises(ValueError, match="daily_capture_plan_invalid"):
        validate_capture_semantics(context, changed)
    changed_storage = {
        "contract_version": "open_intelligence_source_capture_storage_v2",
        "grant": {"source_estate_digest": "f" * 64},
    }
    with pytest.raises(ValueError, match="daily_capture_storage_invalid"):
        validate_capture_semantics(
            context, dict(artifacts, storage_policy=canonical_bytes(changed_storage))
        )


def test_terminal_chain_requires_observation_and_native_bindings():
    _context, _artifacts, observation, parts = _fixture()
    chain = read_daily_execution_chain(
        derivation_id=DERIVATION_ID, clients=ReadClients(parts, observation)
    )
    assert chain.native_execution["execution_name"] == EXECUTION
    assert chain.result["status"] == "succeeded"


def test_missing_result_is_unavailable_but_native_mismatch_is_integrity():
    _context, _artifacts, observation, parts = _fixture()
    clients = ReadClients(parts, observation)
    clients.parts = dict(parts)

    def no_result(*_args):
        return dict(parts, operation_payload={}, result=None)

    clients.read_chain = no_result
    with pytest.raises(DailyAuthorityUnavailable, match="daily_result_unavailable"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)

    clients = ReadClients(parts, observation)
    clients.read_native_execution = lambda _name: {"execution_name": "wrong"}
    with pytest.raises(DailyAuthorityIntegrityError, match="daily_native_execution_mismatch"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)


def test_malformed_protected_summary_stays_integrity_and_nonterminal_native_is_unavailable():
    _context, _artifacts, observation, parts = _fixture()
    clients = ReadClients(parts, observation)
    clients.read_derivation = lambda _id: {"wrong": "shape"}
    with pytest.raises(DailyAuthorityIntegrityError, match="daily_derivation_read_invalid"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)

    clients = ReadClients(parts, observation)
    native = clients.read_native_execution(EXECUTION)
    native["terminal_state"] = "running"
    clients.read_native_execution = lambda _name: native
    with pytest.raises(DailyAuthorityUnavailable, match="daily_native_execution_nonterminal"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)

    clients = ReadClients(parts, observation)
    clients.read_execution_observation = lambda _name: (_ for _ in ()).throw(
        DailyAuthorityIntegrityError("storage_digest_invalid")
    )
    with pytest.raises(DailyAuthorityIntegrityError, match="storage_digest_invalid"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)


def test_metered_native_adapter_refuses_before_transport_when_budget_is_empty():
    calls = []

    class Routines:
        def call(self, *_args):
            calls.append("query")

    class Objects:
        def read(self, *_args):
            calls.append("object")

    class Native:
        def read(self, *_args):
            calls.append("native")

    adapter = NativeDailyAuthorityReadAdapter(
        routines=Routines(),
        objects=Objects(),
        native=Native(),
        budget=DailyAuthorityBudget(
            queries=0, billed_bytes=0, object_reads=0, object_bytes=0, native_reads=0
        ),
    )
    with pytest.raises(DailyAuthorityUnavailable, match="daily_query_budget_exhausted"):
        adapter.read_derivation(DERIVATION_ID)
    with pytest.raises(DailyAuthorityUnavailable, match="daily_object_budget_exhausted"):
        adapter.read_execution_observation(EXECUTION)
    with pytest.raises(DailyAuthorityUnavailable, match="daily_native_budget_exhausted"):
        adapter.read_native_execution(EXECUTION)
    assert calls == []


@pytest.mark.parametrize(
    "change,effect,spend",
    [
        ({"complete": False, "total_bytes_billed": None}, "effects_recorded", "measured"),
        ({"total_bytes_billed": 1}, "no_effect", "no_spend"),
        ({"storage_write_bytes": 1}, "no_effect", "no_spend"),
        ({"query_count": True}, "effects_recorded", "measured"),
    ],
)
def test_invalid_metering_cannot_reach_result_transport(change, effect, spend):
    calls = []

    class Client:
        def record_result(self, *args):
            calls.append(args)
            return {"derivation_id": args[0], "consumption_id": args[1], "payload_digest": args[3]}

    metering = {
        "query_count": 0,
        "total_bytes_billed": 0,
        "storage_write_count": 0,
        "storage_write_bytes": 0,
        "vendor_credits": "0",
        "model_calls": 0,
        "complete": True,
    }
    metering.update(change)
    payload = _canonical({"contract_version": "test_payload_v1"})
    with pytest.raises(DailyAuthorityIntegrityError):
        record_daily_result(
            derivation_id=DERIVATION_ID,
            consumption_id="exc_" + HEX,
            canonical_payload_json=payload,
            payload_digest=hashlib.sha256(payload.encode()).hexdigest(),
            execution_observation_sha256=HEX,
            canonical_stage_metering_json=_canonical(metering),
            result_reference="test-result",
            terminal_state="failed",
            effect_state=effect,
            spend_state=spend,
            clients=Client(),
        )
    assert calls == []


@pytest.mark.parametrize(
    "observed,created",
    [
        ("2099-01-01T00:00:00+00:00", "2026-09-14T00:01:03+00:00"),
        ("2026-09-14T00:01:02+00:00", "2026-09-14T00:01:01+00:00"),
        ("2026-09-14T00:01:02+00:00", "2026-09-14T00:01:03"),
        ("2026-09-14T00:01:03.0000001+00:00", "2026-09-14T00:01:03+00:00"),
        ("2026-09-14T00:01:02+00:00", None),
    ],
)
def test_observation_chronology_refuses_before_sql_consumption(monkeypatch, observed, created):
    from src.analysis.open_intelligence import daily_execution_authority as subject

    monkeypatch.setattr(
        subject, "_utc_now", lambda: datetime(2026, 9, 14, 0, 2, tzinfo=UTC), raising=False
    )
    context, artifacts, observation, parts = _fixture()
    value = json.loads(observation)
    value["observed_at"] = observed
    observation = _canonical(value)
    parts["consumption"]["consumption_id"] = (
        "exc_"
        + hashlib.sha256(
            canonical_bytes(
                {
                    "consumed_at": CONSUMED_AT,
                    "consumption_contract_version": "open_intelligence_execution_consumption_v3",
                    "derivation_id": DERIVATION_ID,
                    "execution_name": EXECUTION,
                    "execution_observation_sha256": hashlib.sha256(
                        observation.encode()
                    ).hexdigest(),
                    "origin_registry_sha256": ORIGIN,
                    "resource_manifest_sha256": RESOURCE,
                }
            )
        ).hexdigest()
    )

    class Client(WriteClients):
        def admit_execution_observation(self, *args):
            raw, metadata = super().admit_execution_observation(*args)
            return raw, dict(metadata, created_at=created)

    client = Client(parts)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"observation.*time"):
        consume_daily_derivation(
            derivation_id=DERIVATION_ID,
            execution_name=EXECUTION,
            canonical_operation_context_json=context,
            canonical_operation_artifact_set_json=artifacts,
            canonical_execution_observation_json=observation,
            execution_observation_sha256=hashlib.sha256(observation.encode()).hexdigest(),
            clients=client,
        )
    assert not any(call[0] == DERIVATION_ID for call in client.calls)


def test_zero_readback_budget_prevents_observation_write():
    calls = []

    class Objects:
        def write_once(self, *args):
            calls.append("write")

        def read(self, *args):
            calls.append("read")

    adapter = NativeDailyAuthorityWriteAdapter(
        routines=object(),
        objects=Objects(),
        native=object(),
        budget=DailyAuthorityBudget(
            queries=0, billed_bytes=0, object_reads=0, object_bytes=0, native_reads=0
        ),
    )
    with pytest.raises(DailyAuthorityUnavailable):
        adapter.admit_execution_observation(EXECUTION, b"{}", hashlib.sha256(b"{}").hexdigest())
    assert calls == []


@pytest.mark.parametrize(
    "exhausted", ["object_reads", "object_bytes", "object_writes", "object_write_bytes"]
)
def test_observation_reserves_both_effect_and_readback_before_creation(exhausted):
    calls = []
    body = b"{}"

    class Objects:
        def write_once(self, *args):
            calls.append("write")

        def read(self, *args):
            calls.append("read")

    limits = {
        "queries": 1,
        "billed_bytes": 100,
        "object_reads": 1,
        "object_bytes": len(body),
        "native_reads": 2,
        "object_writes": 1,
        "object_write_bytes": len(body),
    }
    limits[exhausted] = 0
    budget = DailyAuthorityBudget(**limits)
    adapter = NativeDailyAuthorityWriteAdapter(
        routines=object(), objects=Objects(), native=object(), budget=budget
    )
    with pytest.raises(DailyAuthorityUnavailable):
        adapter.admit_execution_observation(EXECUTION, body, hashlib.sha256(body).hexdigest())
    assert calls == []
    assert all(getattr(budget, key) == value for key, value in limits.items())


def test_observation_round_trip_uses_reserved_bytes_and_debits_write_once():
    body = b"{}"
    calls = []

    class Objects:
        def write_once(self, name, value):
            calls.append(("write", value))

        def read(self, name, maximum_bytes):
            calls.append(("read", maximum_bytes))
            return body, {}

    budget = DailyAuthorityBudget(
        queries=1,
        billed_bytes=100,
        object_reads=1,
        object_bytes=len(body),
        native_reads=2,
        object_writes=1,
        object_write_bytes=len(body),
    )
    adapter = NativeDailyAuthorityWriteAdapter(
        routines=object(), objects=Objects(), native=object(), budget=budget
    )
    assert (
        adapter.admit_execution_observation(EXECUTION, body, hashlib.sha256(body).hexdigest())[0]
        == body
    )
    assert calls == [("write", body), ("read", len(body))]
    assert (
        budget.object_reads,
        budget.object_bytes,
        budget.object_writes,
        budget.object_write_bytes,
    ) == (0, 0, 0, 0)


def _stored_result():
    _context, _artifacts, observation, parts = _fixture()
    reader = ReadClients(parts, observation)
    digest = hashlib.sha256(observation.encode()).hexdigest()
    chain = reader.read_chain(DERIVATION_ID, observation, digest)
    return parts, chain["result"], chain["operation_payload"], digest


def _record_stored_result(row, payload, observation_digest):
    class Transport:
        def call(self, routine, parameters):
            assert routine == "sp_record_open_intelligence_daily_result_v1"
            assert parameters[:2] == (DERIVATION_ID, row["consumption_id"])
            return {"result": row}, 0

    adapter = NativeDailyAuthorityWriteAdapter(
        routines=Transport(),
        objects=None,
        native=None,
        budget=DailyAuthorityBudget(
            queries=1, billed_bytes=1_000_000, object_reads=0, object_bytes=0, native_reads=0
        ),
    )
    envelope = json.loads(row["canonical_result_json"])
    return record_daily_result(
        derivation_id=DERIVATION_ID,
        consumption_id=row["consumption_id"],
        canonical_payload_json=_canonical(payload),
        payload_digest=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        execution_observation_sha256=observation_digest,
        canonical_stage_metering_json=_canonical(envelope["stage_metering"]),
        result_reference="gs://protected/result.json",
        terminal_state="succeeded",
        effect_state="effects_recorded",
        spend_state="measured",
        clients=adapter,
    )


def test_result_accepts_the_physical_row_without_invented_alias_columns():
    _parts, row, payload, digest = _stored_result()
    assert "derivation_id" not in row
    assert "payload_digest" not in row
    result = _record_stored_result(row, payload, digest)
    assert result["result_id"] == row["result_id"]
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("effect", ["measured", "none"])
def test_retired_effect_spellings_refuse(effect):
    from src.analysis.open_intelligence.daily_execution_contracts import (
        DailyContractError,
        validate_result_metering,
    )

    metering = {
        "complete": True,
        "model_calls": 0,
        "query_count": 0,
        "storage_write_bytes": 0,
        "storage_write_count": 0,
        "total_bytes_billed": 0,
        "vendor_credits": "0",
    }
    with pytest.raises(DailyContractError, match="daily_result_state_invalid"):
        validate_result_metering(
            metering, terminal_state="failed", effect_state=effect, spend_state="no_spend"
        )


def test_result_refuses_a_validly_hashed_but_different_committed_payload():
    _parts, row, payload, digest = _stored_result()
    envelope = json.loads(row["canonical_result_json"])
    envelope["operation_payload"] = {**payload, "value": "substituted"}
    envelope["payload_digest"] = hashlib.sha256(
        canonical_bytes(envelope["operation_payload"])
    ).hexdigest()
    row["canonical_result_json"] = _canonical(envelope)
    row["result_digest"] = hashlib.sha256(row["canonical_result_json"].encode()).hexdigest()
    row["result_id"] = "exr_" + row["result_digest"]
    with pytest.raises(DailyAuthorityIntegrityError, match="readback_mismatch"):
        _record_stored_result(row, payload, digest)


def test_native_completion_after_result_commit_is_not_reversed():
    _context, _artifacts, observation, parts = _fixture()
    reader = ReadClients(parts, observation)
    original = reader.read_native_execution
    reader.read_native_execution = lambda name: {
        **original(name),
        "completed_at": "2026-09-14T00:02:01.000000123Z",
    }
    chain = read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=reader)
    assert chain.result["completed_at"] == "2026-09-14T00:02:00+00:00"
    assert chain.effective_available_at == "2026-09-14T00:02:01.000000123Z"


def test_native_completion_before_consumption_refuses():
    _context, _artifacts, observation, parts = _fixture()
    reader = ReadClients(parts, observation)
    original = reader.read_native_execution
    reader.read_native_execution = lambda name: {
        **original(name),
        "completed_at": "2026-09-14T00:01:02Z",
    }
    with pytest.raises(DailyAuthorityIntegrityError, match="native_terminal_mismatch"):
        read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=reader)


def test_read_adapter_preserves_canonical_payload_numbers_from_sql_string_column():
    _context, _artifacts, observation, parts = _fixture()
    digest = hashlib.sha256(observation.encode()).hexdigest()
    row = ReadClients(parts, observation).read_chain(DERIVATION_ID, observation, digest)
    payload = {"contract_version": "payload_v1", "ratio": 1.0, "wide": 9007199254740993}
    row.pop("operation_payload")
    row["canonical_operation_payload_json"] = _canonical(payload)

    class Transport:
        def call(self, routine, parameters):
            assert routine == "sp_read_open_intelligence_daily_chain_v1"
            assert parameters == (DERIVATION_ID, observation, digest)
            return row, 0

    adapter = NativeDailyAuthorityReadAdapter(
        routines=Transport(),
        objects=None,
        native=None,
        budget=DailyAuthorityBudget(
            queries=1, billed_bytes=1_000_000, object_reads=0, object_bytes=0, native_reads=0
        ),
    )
    decoded = adapter.read_chain(DERIVATION_ID, observation, digest)
    assert canonical_bytes(decoded["operation_payload"]) == canonical_bytes(payload)
    assert type(decoded["operation_payload"]["ratio"]) is float
    assert decoded["operation_payload"]["wide"] == 9007199254740993
    assert "canonical_operation_payload_json" not in decoded


def test_native_instant_order_preserves_submicrosecond_fractions():
    earlier = native_instant_key("2026-09-14T00:02:00.123456001Z")
    later = native_instant_key("2026-09-14T00:02:00.123456002+00:00")
    assert earlier < later
    assert earlier == native_instant_key("2026-09-14T00:02:00.123456001000Z")
    with pytest.raises(DailyAuthorityIntegrityError):
        native_instant_key("2026-02-30T00:00:00Z")
