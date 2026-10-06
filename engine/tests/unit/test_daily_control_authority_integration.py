import base64
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import STEP_NUMBERS_V2
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityBudget,
    DailyAuthorityIntegrityError,
    DailyAuthorityUnavailable,
    NativeDailyAuthorityReadAdapter,
    NativeDailyAuthorityWriteAdapter,
    consume_daily_derivation,
    read_daily_execution_chain,
    record_daily_result,
)
from src.analysis.open_intelligence.daily_native_transports import CloudRunTransport
from src.analysis.open_intelligence.daily_store import (
    DailyStore,
    StoreRefusal,
    slot_operation_id_v2,
    unit_request_id_v2,
)

from tests.unit.test_daily_store import FakeObjectClient

NOW = datetime(2026, 9, 14, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 9, 14, tzinfo=UTC)
ESTATE = "intelligence-42-core"
PARENT = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging/executions/parent-1"
JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
CHILD = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
EXECUTION = JOB + "/executions/child-1"
IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:" + "1" * 64
GRANT = "2" * 64
POLICY = "3" * 64
APPROVAL = "exa_" + "4" * 64
DERIVATION = "exd_" + "5" * 64
ORIGIN = "6" * 64
RESOURCE = "7" * 64


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def slot():
    return slot_operation_id_v2(environment="staging", source_estate_id=ESTATE, cutoff_utc=CUTOFF)


def intent(context, input_digest):
    attempt = 1
    return {
        "intent_id": "dsi_"
        + digest(
            {
                "attempt_version": attempt,
                "control_contract_version": "42_daily_control_v2",
                "slot_id": slot(),
                "stage": "collection",
            }
        ),
        "business_attempt_id": "bat_" + digest({"attempt": "collection@1", "operation_id": slot()}),
        "stage": "collection",
        "attempt_version": attempt,
        "input_digest": input_digest,
        "operation_context_sha256": digest(context),
        "authorizing_approval_id": APPROVAL,
        "authorizing_grant_digest": GRANT,
        "child_job_resource": JOB,
        "lease_owner": PARENT,
        "lease_epoch": 1,
        "phase": "prepared",
        "derivation_id": None,
        "dispatch_observation_reference": None,
        "resolution_reference": None,
    }


class ObservationObjects:
    def __init__(self, created_at):
        self.created_at = created_at
        self.values = {}
        self.events = []

    def write_once(self, name, body):
        self.events.append("observation_write")
        if name in self.values:
            raise FileExistsError(name)
        self.values[name] = bytes(body)

    def read(self, name, maximum_bytes):
        self.events.append("observation_read")
        body = self.values[name]
        assert len(body) <= maximum_bytes
        return body, {
            "content_sha256": hashlib.sha256(body).hexdigest(),
            "created_at": self.created_at,
            "generation": "1",
            "object_name": name,
            "size_bytes": len(body),
        }


class Native:
    def __init__(self, context, observation, completed_at):
        self.context = context
        self.observation = observation
        self.completed_at = completed_at

    def read_job(self, resource):
        return {
            "grant_digest": GRANT,
            "image_uri": IMAGE,
            "job_policy_digest": POLICY,
            "job_resource": resource,
            "service_identity": CHILD,
        }

    def read(self, execution):
        return {
            "completed_at": self.completed_at,
            "execution_created_at": self.observation["execution_created_at"],
            "execution_name": execution,
            "execution_started_at": self.observation["execution_started_at"],
            "grant_digest": GRANT,
            "image_uri": IMAGE,
            "job_policy_digest": POLICY,
            "job_resource": JOB,
            "service_identity": CHILD,
            "step_number": STEP_NUMBERS_V2[self.context["stage"]],
            "terminal_state": "succeeded",
        }


class Routines:
    def __init__(self, parts, payload):
        self.parts = parts
        self.payload = payload
        self.result = None
        self.events = []

    def call(self, routine, parameters):
        self.events.append(routine)
        if routine == "sp_consume_open_intelligence_daily_derivation_v1":
            return copy.deepcopy(self.parts), 0
        if routine == "sp_record_open_intelligence_daily_result_v1":
            (
                derivation_id,
                consumption_id,
                payload_json,
                payload_digest,
                observation_digest,
                metering_json,
                result_reference,
                terminal_state,
                effect_state,
                spend_state,
            ) = parameters
            completed_at = "2026-09-14T01:00:02.123456Z"
            envelope = {
                "authorizing_approval_id": APPROVAL,
                "authorizing_grant_digest": GRANT,
                "business_attempt_id": self.parts["derivation"]["business_attempt_id"],
                "child_job_resource": JOB,
                "completed_at": completed_at,
                "consumption_id": consumption_id,
                "contract_version": "daily_execution_result_v1",
                "derivation_id": derivation_id,
                "effect_state": effect_state,
                "execution_name": EXECUTION,
                "execution_observation_sha256": observation_digest,
                "manifest_sha256": self.parts["derivation"]["manifest_sha256"],
                "operation": "daily_source_collection",
                "operation_context_sha256": self.parts["derivation"]["operation_context_sha256"],
                "operation_payload": json.loads(payload_json),
                "payload_contract_version": self.payload["contract_version"],
                "payload_digest": payload_digest,
                "result_reference": result_reference,
                "spend_state": spend_state,
                "stage_metering": json.loads(metering_json),
                "terminal_state": terminal_state,
            }
            raw = canonical_bytes(envelope).decode()
            result_digest = hashlib.sha256(raw.encode()).hexdigest()
            self.result = {
                "approval_id": derivation_id,
                "canonical_result_json": raw,
                "completed_at": completed_at,
                "consumption_id": consumption_id,
                "execution_name": EXECUTION,
                "manifest_sha256": self.parts["derivation"]["manifest_sha256"],
                "operation": "daily_source_collection",
                "origin_registry_sha256": ORIGIN,
                "resource_manifest_sha256": RESOURCE,
                "result_contract_version": "open_intelligence_execution_result_v3",
                "result_digest": result_digest,
                "result_id": "exr_" + result_digest,
                "result_reference": result_reference,
                "status": terminal_state,
            }
            return {"result": copy.deepcopy(self.result)}, 0
        if routine == "sp_read_open_intelligence_daily_derivation_v1":
            return {
                "consumption": copy.deepcopy(self.parts["consumption"]),
                "derivation": copy.deepcopy(self.parts["derivation"]),
                "lifecycle_state": "consumed",
                "tombstone": None,
            }, 0
        if routine == "sp_read_open_intelligence_daily_chain_v1":
            return dict(
                copy.deepcopy(self.parts),
                canonical_operation_payload_json=canonical_bytes(self.payload).decode(),
                result=copy.deepcopy(self.result),
            ), 0
        raise AssertionError(routine)


def fixture(*, created_at="2026-09-14T01:00:00Z", started_at="2026-09-14T01:00:00.1Z"):
    source_policy = canonical_bytes({"contract_version": "source_policy_v1"})
    artifact_set = {
        "artifacts": [
            {
                "data": base64.b64encode(source_policy).decode(),
                "encoding": "base64",
                "name": "source_policy",
            }
        ],
        "contract_version": "daily_operation_artifact_set_v1",
    }
    artifact_json = canonical_bytes(artifact_set).decode()
    input_digest = "8" * 64
    context = {
        "attempt_version": 1,
        "authorizing_approval_id": APPROVAL,
        "authorizing_grant_digest": GRANT,
        "business_attempt_id": "bat_" + digest({"attempt": "collection@1", "operation_id": slot()}),
        "child_image_uri": IMAGE,
        "child_job_policy_digest": POLICY,
        "child_job_resource": JOB,
        "child_service_identity": CHILD,
        "contract_version": "daily_operation_context_v1",
        "cutoff_utc": CUTOFF.isoformat(),
        "execution_observation_prefix": "42/daily/execution-observations/",
        "gcs_control_object": f"42/daily/slots/{slot()}/control.json",
        "input_digest": input_digest,
        "intent_id": "dsi_"
        + digest(
            {
                "attempt_version": 1,
                "control_contract_version": "42_daily_control_v2",
                "slot_id": slot(),
                "stage": "collection",
            }
        ),
        "lease_epoch": 1,
        "lease_owner": PARENT,
        "mode": "run",
        "operation": "daily_source_collection",
        "operation_artifact_set_sha256": hashlib.sha256(artifact_json.encode()).hexdigest(),
        "parent_execution_name": PARENT,
        "parent_job_resource": PARENT.rsplit("/executions/", 1)[0],
        "parent_principal": "intelligence-42-daily-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
        "predecessor_result_digest": None,
        "predecessor_result_id": None,
        "profile_digest": "9" * 64,
        "slot_id": slot(),
        "source_estate_id": ESTATE,
        "stage": "collection",
    }
    context_json = canonical_bytes(context).decode()
    manifest = {
        "input_artifacts": [
            {
                "name": "operation_context",
                "sha256": hashlib.sha256(context_json.encode()).hexdigest(),
            },
            {"name": "source_policy", "sha256": hashlib.sha256(source_policy).hexdigest()},
        ],
        "limits": {"max_credits": 620},
    }
    manifest_json = canonical_bytes(manifest).decode()
    observation = {
        "authorizing_grant_digest": GRANT,
        "child_image_uri": IMAGE,
        "child_job_policy_digest": POLICY,
        "child_job_resource": JOB,
        "child_service_identity": CHILD,
        "contract_version": "daily_native_execution_observation_v1",
        "derivation_id": DERIVATION,
        "execution_created_at": created_at,
        "execution_name": EXECUTION,
        "execution_started_at": started_at,
        "observed_at": "2026-09-14T01:00:00.2Z",
        "observer_principal": CHILD,
    }
    observation_json = canonical_bytes(observation).decode()
    observation_digest = hashlib.sha256(observation_json.encode()).hexdigest()
    consumed_at = "2026-09-14T01:00:01Z"
    consumption_id = "exc_" + digest(
        {
            "consumed_at": consumed_at,
            "consumption_contract_version": "open_intelligence_execution_consumption_v3",
            "derivation_id": DERIVATION,
            "execution_name": EXECUTION,
            "execution_observation_sha256": observation_digest,
            "origin_registry_sha256": ORIGIN,
            "resource_manifest_sha256": RESOURCE,
        }
    )
    derivation = {
        "authorizing_approval_id": APPROVAL,
        "authorizing_grant_digest": GRANT,
        "business_attempt_id": context["business_attempt_id"],
        "canonical_manifest_json": manifest_json,
        "canonical_operation_context_json": context_json,
        "child_job_resource": JOB,
        "derivation_id": DERIVATION,
        "manifest_sha256": hashlib.sha256(manifest_json.encode()).hexdigest(),
        "operation": "daily_source_collection",
        "operation_context_sha256": hashlib.sha256(context_json.encode()).hexdigest(),
    }
    consumption = {
        "approval_id": DERIVATION,
        "consumed_at": consumed_at,
        "consumption_contract_version": "open_intelligence_execution_consumption_v3",
        "consumption_id": consumption_id,
        "execution_name": EXECUTION,
        "operation": "daily_source_collection",
        "origin_registry_sha256": ORIGIN,
        "resource_manifest_sha256": RESOURCE,
    }
    parts = {
        "authorizing_approval": {"approval_id": APPROVAL, "manifest_sha256": GRANT},
        "consumption": consumption,
        "derivation": derivation,
        "grant": {
            "allowed_operations": ["daily_source_collection"],
            "contract_version": "42_recurring_execution_grant_v2",
            "grant_id": "grant-1",
            "source_policy_digest": hashlib.sha256(source_policy).hexdigest(),
        },
        "manifest": manifest,
        "operation_context": context,
    }
    payload = {"contract_version": "daily_source_collection_result_v1", "rows": 1}
    return (
        context,
        context_json,
        artifact_json,
        observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    )


def budget():
    return DailyAuthorityBudget(
        queries=8,
        billed_bytes=1000,
        object_reads=4,
        object_bytes=100000,
        native_reads=4,
        object_writes=2,
        object_write_bytes=100000,
    )


def test_real_authority_capability_drives_unit_and_terminal_control_paths():
    (
        context,
        context_json,
        artifacts,
        observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    ) = fixture()
    control_objects = FakeObjectClient()
    parent = DailyStore(control_objects, owner=PARENT, now=lambda: NOW, lease_seconds=60)
    assert parent.claim_v2(slot(), "staging", ESTATE, CUTOFF)
    pending = intent(context, context["input_digest"])
    parent.prepare_intent_v2(slot(), pending)
    parent.bind_derivation_v2(
        slot(),
        {
            "derivation_id": DERIVATION,
            "operation_context_sha256": pending["operation_context_sha256"],
        },
    )
    parent.mark_dispatch_started_v2(slot())
    lease_before = copy.deepcopy(control_objects.control(slot())["lease"])

    observation_objects = ObservationObjects("2026-09-14T01:00:00.3Z")
    native = Native(context, observation, "2026-09-14T01:00:02.1234567Z")
    routines = Routines(parts, payload)
    write = NativeDailyAuthorityWriteAdapter(
        routines=routines, objects=observation_objects, native=native, budget=budget()
    )
    consumed = consume_daily_derivation(
        derivation_id=DERIVATION,
        execution_name=EXECUTION,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=artifacts,
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=observation_digest,
        clients=write,
    )

    child = DailyStore(control_objects, owner=EXECUTION, now=lambda: NOW + timedelta(seconds=4))
    request = {
        "route": "twitter/profile",
        "market": "za",
        "subject": "brand_sa",
        "window_start": (CUTOFF - timedelta(days=1)).isoformat(),
        "window_end": CUTOFF.isoformat(),
        "request_sha256": "a" * 64,
    }
    permit = {
        "contract_version": "daily_collection_unit_permit_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": unit_request_id_v2(request),
        **request,
        "quoted_credits": "1",
        "max_calls": 1,
        "permit_sequence": 1,
        "created_at": "2000-01-01T00:00:00+00:00",
    }
    durable = child.publish_unit_permit_v2(slot(), permit, consumed_authority=consumed)
    http_effects = ["GET"]
    assert durable["created_at"] == (NOW + timedelta(seconds=4)).isoformat()
    assert http_effects == ["GET"]
    unit_result = {
        "contract_version": "daily_collection_unit_result_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": permit["unit_id"],
        "permit_sha256": digest(durable),
        "state": "complete",
        "calls": 1,
        "charged_credits": "1",
        "response_sha256": "b" * 64,
        "completed_at": (NOW + timedelta(seconds=4)).isoformat(),
        "reason": None,
    }
    stored_unit = child.publish_unit_result_v2(slot(), unit_result, consumed_authority=consumed)
    assert stored_unit["state"] == "complete"
    assert control_objects.control(slot())["lease"] == lease_before

    metering = {
        "complete": True,
        "model_calls": 0,
        "query_count": 0,
        "storage_write_bytes": 1,
        "storage_write_count": 1,
        "total_bytes_billed": 0,
        "vendor_credits": "2",
    }
    physical = record_daily_result(
        derivation_id=DERIVATION,
        consumption_id=consumed.consumption_id,
        canonical_payload_json=canonical_bytes(payload).decode(),
        payload_digest=digest(payload),
        execution_observation_sha256=observation_digest,
        canonical_stage_metering_json=canonical_bytes(metering).decode(),
        result_reference="gs://protected/result.json",
        terminal_state="succeeded",
        effect_state="effects_recorded",
        spend_state="measured",
        clients=write,
    )
    assert set(physical) >= {"status", "canonical_result_json", "result_id"}
    read = NativeDailyAuthorityReadAdapter(
        routines=routines, objects=observation_objects, native=native, budget=budget()
    )
    chain = read_daily_execution_chain(derivation_id=DERIVATION, clients=read)
    assert chain.effective_available_at == "2026-09-14T01:00:02.1234567Z"
    before = len(control_objects.writes)
    admitted = parent.publish_terminal_v2(slot(), "collection", DERIVATION, clients=read)
    assert admitted["state"] == "succeeded"
    assert control_objects.control(slot())["dispatch_intent"] is None
    assert len(control_objects.writes) == before + 2


@pytest.mark.parametrize("mutation", ["context", "execution", "policy"])
def test_wrong_authority_binding_refuses_before_effect(mutation):
    (
        context,
        context_json,
        artifacts,
        observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    ) = fixture()
    if mutation == "context":
        parts["operation_context"]["input_digest"] = "f" * 64
    native = Native(context, observation, "2026-09-14T01:00:02.1234567Z")
    if mutation == "execution":
        native.read = lambda name: {"execution_name": name, "job_resource": "wrong"}
    if mutation == "policy":
        native.read_job = lambda resource: {
            "grant_digest": GRANT,
            "image_uri": IMAGE,
            "job_policy_digest": "f" * 64,
            "job_resource": resource,
            "service_identity": CHILD,
        }
    routines = Routines(parts, payload)
    write = NativeDailyAuthorityWriteAdapter(
        routines=routines,
        objects=ObservationObjects("2026-09-14T01:00:00.3Z"),
        native=native,
        budget=budget(),
    )
    with pytest.raises((DailyAuthorityIntegrityError, DailyAuthorityUnavailable)):
        consume_daily_derivation(
            derivation_id=DERIVATION,
            execution_name=EXECUTION,
            canonical_operation_context_json=context_json,
            canonical_operation_artifact_set_json=artifacts,
            canonical_execution_observation_json=observation_json,
            execution_observation_sha256=observation_digest,
            clients=write,
        )
    assert (
        mutation == "context"
        or "sp_consume_open_intelligence_daily_derivation_v1" not in routines.events
    )


@pytest.mark.parametrize("settle_unknown", [False, True])
def test_unknown_unit_blocks_a_different_paid_unit_before_effect(settle_unknown):
    (
        context,
        context_json,
        artifacts,
        observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    ) = fixture()
    control_objects = FakeObjectClient()
    parent = DailyStore(control_objects, owner=PARENT, now=lambda: NOW, lease_seconds=60)
    assert parent.claim_v2(slot(), "staging", ESTATE, CUTOFF)
    pending = intent(context, context["input_digest"])
    parent.prepare_intent_v2(slot(), pending)
    parent.bind_derivation_v2(
        slot(),
        {
            "derivation_id": DERIVATION,
            "operation_context_sha256": pending["operation_context_sha256"],
        },
    )
    parent.mark_dispatch_started_v2(slot())
    write = NativeDailyAuthorityWriteAdapter(
        routines=Routines(parts, payload),
        objects=ObservationObjects("2026-09-14T01:00:00.3Z"),
        native=Native(context, observation, "2026-09-14T01:00:02Z"),
        budget=budget(),
    )
    consumed = consume_daily_derivation(
        derivation_id=DERIVATION,
        execution_name=EXECUTION,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=artifacts,
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=observation_digest,
        clients=write,
    )
    child = DailyStore(control_objects, owner=EXECUTION, now=lambda: NOW + timedelta(seconds=4))
    request = {
        "route": "twitter/profile",
        "market": "za",
        "subject": "brand_sa",
        "window_start": (CUTOFF - timedelta(days=1)).isoformat(),
        "window_end": CUTOFF.isoformat(),
        "request_sha256": "a" * 64,
    }
    permit = {
        "contract_version": "daily_collection_unit_permit_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": unit_request_id_v2(request),
        **request,
        "quoted_credits": "1",
        "max_calls": 1,
        "permit_sequence": 1,
        "created_at": "ignored",
    }
    first = child.publish_unit_permit_v2(slot(), permit, consumed_authority=consumed)
    unknown = {
        "contract_version": "daily_collection_unit_result_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": permit["unit_id"],
        "permit_sha256": digest(first),
        "state": "unknown",
        "calls": 1,
        "charged_credits": None,
        "response_sha256": None,
        "completed_at": (NOW + timedelta(seconds=4)).isoformat(),
        "reason": "transport_lost",
    }
    if settle_unknown:
        assert (
            child.publish_unit_result_v2(slot(), unknown, consumed_authority=consumed)["state"]
            == "unknown"
        )
    second_request = dict(request, request_sha256="c" * 64)
    second = dict(
        permit,
        unit_id=unit_request_id_v2(second_request),
        request_sha256="c" * 64,
        permit_sequence=2,
    )
    before = copy.deepcopy(control_objects.objects)
    with pytest.raises(StoreRefusal, match="unit_state_unknown"):
        child.publish_unit_permit_v2(slot(), second, consumed_authority=consumed)
    assert control_objects.objects == before


def test_unknown_charge_refuses_a_second_paid_request_for_the_same_unit():
    (
        context,
        context_json,
        artifacts,
        observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    ) = fixture()
    control_objects = FakeObjectClient()
    parent = DailyStore(control_objects, owner=PARENT, now=lambda: NOW, lease_seconds=60)
    assert parent.claim_v2(slot(), "staging", ESTATE, CUTOFF)
    pending = intent(context, context["input_digest"])
    parent.prepare_intent_v2(slot(), pending)
    parent.bind_derivation_v2(
        slot(),
        {
            "derivation_id": DERIVATION,
            "operation_context_sha256": pending["operation_context_sha256"],
        },
    )
    parent.mark_dispatch_started_v2(slot())
    write = NativeDailyAuthorityWriteAdapter(
        routines=Routines(parts, payload),
        objects=ObservationObjects("2026-09-14T01:00:00.3Z"),
        native=Native(context, observation, "2026-09-14T01:00:02Z"),
        budget=budget(),
    )
    consumed = consume_daily_derivation(
        derivation_id=DERIVATION,
        execution_name=EXECUTION,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=artifacts,
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=observation_digest,
        clients=write,
    )
    child = DailyStore(control_objects, owner=EXECUTION, now=lambda: NOW + timedelta(seconds=4))
    request = {
        "route": "twitter/profile",
        "market": "za",
        "subject": "brand_sa",
        "window_start": (CUTOFF - timedelta(days=1)).isoformat(),
        "window_end": CUTOFF.isoformat(),
        "request_sha256": "a" * 64,
    }
    permit = {
        "contract_version": "daily_collection_unit_permit_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": unit_request_id_v2(request),
        **request,
        "quoted_credits": "1",
        "max_calls": 1,
        "permit_sequence": 1,
        "created_at": "ignored",
    }
    first = child.publish_unit_permit_v2(slot(), permit, consumed_authority=consumed)
    unknown = {
        "contract_version": "daily_collection_unit_result_v1",
        "operation_id": slot(),
        "consumption_id": consumed.consumption_id,
        "unit_id": permit["unit_id"],
        "permit_sha256": digest(first),
        "state": "unknown",
        "calls": 1,
        "charged_credits": None,
        "response_sha256": None,
        "completed_at": (NOW + timedelta(seconds=4)).isoformat(),
        "reason": "transport_lost",
    }
    settled = child.publish_unit_result_v2(slot(), unknown, consumed_authority=consumed)
    assert settled["state"] == "unknown"
    assert settled["reason"] == "unit_charge_unknown"
    assert (
        child.read_unit_v2(slot(), permit["unit_id"], consumed_authority=consumed)["state"]
        == "unknown"
    )

    # The charge for this unit is unknown, so buying the same unit again would
    # risk paying twice for one effect. A retry must be refused before it can
    # reach the vendor, and it must leave the control record untouched.
    before = copy.deepcopy(control_objects.objects)
    retry = dict(permit, permit_sequence=2)
    with pytest.raises(StoreRefusal, match="unit_already_started"):
        child.publish_unit_permit_v2(slot(), retry, consumed_authority=consumed)
    assert control_objects.objects == before

    # Nor may a byte-identical replay of the first permit slip past the
    # ledger: the stored orphan permit matches, so only the ledger entry
    # stands between the replay and a second charge.
    identical = dict(permit)
    with pytest.raises(StoreRefusal, match="unit_already_started"):
        child.publish_unit_permit_v2(slot(), identical, consumed_authority=consumed)
    assert control_objects.objects == before


# The chain read over the real Cloud Run transport: the native execution view is built
# from the Cloud Run v2 Execution resource itself, not from a view shaped fake.

CREATED_AT = "2026-09-14T01:00:00.051234+00:00"
STARTED_AT = "2026-09-14T01:00:00.100000+00:00"
COMPLETION_TIME = "2026-09-14T01:00:02.123456789Z"
RUN_API = "https://run.googleapis.com/v2/"


class CloudRunResponse:
    def __init__(self, body):
        self.status_code = 200
        self.body = body

    def json(self):
        return copy.deepcopy(self.body)


class CloudRunSession:
    def __init__(self, execution):
        self.execution = execution

    def get(self, url, *, timeout):
        if url == RUN_API + JOB:
            return CloudRunResponse(cloud_run_job())
        assert url == RUN_API + EXECUTION
        return CloudRunResponse(self.execution)


def _task():
    return {
        "containers": [
            {
                "image": IMAGE,
                "command": ["python"],
                "args": ["-m", "ops.runners.managed_runtime"],
                "env": [
                    {"name": "TRENDS_ENV", "value": "staging"},
                    {"name": "DAILY_STEP_NUMBER", "value": "1"},
                ],
                "resources": {"limits": {"cpu": "1000m", "memory": "512Mi"}},
            }
        ],
        "maxRetries": 0,
        "timeout": "3600s",
        "serviceAccount": CHILD,
        "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
    }


def cloud_run_job():
    task = _task()
    task["containers"][0]["env"] = task["containers"][0]["env"][:1]
    return {
        "name": JOB,
        "template": {
            "annotations": {
                "42.ogilvy/runtime-configuration-sha256": POLICY,
                "42.ogilvy/recurring-grant-sha256": GRANT,
            },
            "taskCount": 1,
            "parallelism": 1,
            "template": task,
        },
    }


def running_execution():
    """A Cloud Run v2 Execution while its one task runs."""
    return {
        "name": EXECUTION,
        "uid": "0b5c1d6e-3f5a-4a39-9d57-3f7e2c1b9a10",
        "generation": "1",
        "annotations": {
            "42.ogilvy/runtime-configuration-sha256": POLICY,
            "42.ogilvy/recurring-grant-sha256": GRANT,
        },
        "createTime": "2026-09-14T01:00:00.051234Z",
        "startTime": "2026-09-14T01:00:00.100000123Z",
        "updateTime": "2026-09-14T01:00:01.500000Z",
        "launchStage": "GA",
        "job": "intelligence-42-daily-staging",
        "parallelism": 1,
        "taskCount": 1,
        "template": _task(),
        "reconciling": True,
        "conditions": [
            {
                "type": "ResourcesAvailable",
                "state": "CONDITION_SUCCEEDED",
                "lastTransitionTime": "2026-09-14T01:00:00.090000Z",
            },
            {
                "type": "Started",
                "state": "CONDITION_SUCCEEDED",
                "lastTransitionTime": "2026-09-14T01:00:00.100000Z",
            },
            {
                "type": "Completed",
                "state": "CONDITION_PENDING",
                "lastTransitionTime": "2026-09-14T01:00:00.100000Z",
            },
        ],
        "observedGeneration": "1",
        "runningCount": 1,
        "logUri": "https://console.cloud.google.com/logs/viewer?project=ogilvy-trends-v2",
        "etag": '"CLXq1bcGEKCQ9ukB/cHJvamVjdHM"',
    }


def completed_execution(state="succeeded"):
    """The same Execution once Cloud Run completed it, as the v2 API reports it."""
    execution = running_execution()
    del execution["reconciling"]
    del execution["runningCount"]
    execution["completionTime"] = COMPLETION_TIME
    execution["updateTime"] = "2026-09-14T01:00:02.200000Z"
    completed = execution["conditions"][2]
    completed["lastTransitionTime"] = COMPLETION_TIME
    if state == "succeeded":
        execution["succeededCount"] = 1
        completed.update(state="CONDITION_SUCCEEDED", message="Execution completed successfully.")
    elif state == "failed":
        execution["failedCount"] = 1
        completed.update(
            state="CONDITION_FAILED",
            message="Task intelligence-42-daily-staging-child-1-task0 failed.",
            executionReason="NON_ZERO_EXIT_CODE",
        )
    else:
        execution["cancelledCount"] = 1
        completed.update(
            state="CONDITION_FAILED",
            message="The execution was cancelled.",
            executionReason="CANCELLED",
        )
    return execution


def _consumed_and_recorded(session):
    (
        _context,
        context_json,
        artifacts,
        _observation,
        observation_json,
        observation_digest,
        parts,
        payload,
    ) = fixture(created_at=CREATED_AT, started_at=STARTED_AT)
    observation_objects = ObservationObjects("2026-09-14T01:00:00.3Z")
    routines = Routines(parts, payload)
    write = NativeDailyAuthorityWriteAdapter(
        routines=routines,
        objects=observation_objects,
        native=CloudRunTransport(session),
        budget=budget(),
    )
    consumed = consume_daily_derivation(
        derivation_id=DERIVATION,
        execution_name=EXECUTION,
        canonical_operation_context_json=context_json,
        canonical_operation_artifact_set_json=artifacts,
        canonical_execution_observation_json=observation_json,
        execution_observation_sha256=observation_digest,
        clients=write,
    )
    metering = {
        "complete": True,
        "model_calls": 0,
        "query_count": 0,
        "storage_write_bytes": 1,
        "storage_write_count": 1,
        "total_bytes_billed": 0,
        "vendor_credits": "2",
    }
    record_daily_result(
        derivation_id=DERIVATION,
        consumption_id=consumed.consumption_id,
        canonical_payload_json=canonical_bytes(payload).decode(),
        payload_digest=digest(payload),
        execution_observation_sha256=observation_digest,
        canonical_stage_metering_json=canonical_bytes(metering).decode(),
        result_reference="gs://protected/result.json",
        terminal_state="succeeded",
        effect_state="effects_recorded",
        spend_state="measured",
        clients=write,
    )
    return NativeDailyAuthorityReadAdapter(
        routines=routines,
        objects=observation_objects,
        native=CloudRunTransport(session),
        budget=budget(),
    )


def test_a_completed_cloud_run_execution_admits_the_chain_through_the_real_transport():
    session = CloudRunSession(running_execution())
    read = _consumed_and_recorded(session)
    session.execution = completed_execution()
    chain = read_daily_execution_chain(derivation_id=DERIVATION, clients=read)
    assert chain.native_execution["terminal_state"] == "succeeded"
    assert chain.native_execution["completed_at"] == COMPLETION_TIME
    assert chain.effective_available_at == COMPLETION_TIME
    assert chain.result["status"] == "succeeded"


def test_a_running_cloud_run_execution_is_not_terminal():
    session = CloudRunSession(running_execution())
    read = _consumed_and_recorded(session)
    with pytest.raises(DailyAuthorityUnavailable, match=r"^daily_native_execution_nonterminal$"):
        read_daily_execution_chain(derivation_id=DERIVATION, clients=read)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_failed_or_cancelled_cloud_run_execution_never_reads_as_succeeded(state):
    session = CloudRunSession(running_execution())
    read = _consumed_and_recorded(session)
    session.execution = completed_execution(state)
    with pytest.raises(DailyAuthorityIntegrityError, match=r"^daily_native_terminal_mismatch$"):
        read_daily_execution_chain(derivation_id=DERIVATION, clients=read)


def test_an_ambiguous_completed_cloud_run_execution_is_an_integrity_refusal():
    session = CloudRunSession(running_execution())
    read = _consumed_and_recorded(session)
    execution = completed_execution()
    execution["taskCount"] = 3
    execution["succeededCount"] = 2
    session.execution = execution
    with pytest.raises(
        DailyAuthorityIntegrityError, match=r"^daily_native_execution_terminal_ambiguous$"
    ):
        read_daily_execution_chain(derivation_id=DERIVATION, clients=read)
