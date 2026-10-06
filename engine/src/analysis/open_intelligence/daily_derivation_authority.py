"""The parent's authority for the v2 daily kernel: one derivation per stage.

``daily_cycle.execute_daily_cycle_v2`` asks its authority to ``prepare`` a stage (the
dispatch intent and the operation context the store publishes before anything is
derived) and then to ``issue`` it. Here ``issue`` derives the stage's child execution
authority through ``sp_derive_open_intelligence_daily_execution_v1`` under the approved
recurring grant v2, and the stage handler then runs one child execution of the daily job
that consumes that derivation and nothing else.

Every value the context and child manifest carry is bound to something outside the
caller's say:

* the child job, identity, datasets, contract and limits come from the registry
  through ``daily_operation_map``;
* the grant comes from the protected read of the grant row, checked against its own
  digest and the profile's grant digest, and must bind this parent's principal, image,
  source commit and job policy;
* the cost policy is the computed ``daily_workload_cost_policy_v1`` for this job policy
  and must be inside its review window. The authority recomputes it with
  ``build_daily_cost_policy`` from its own registry bindings for the operations the policy
  prices, the rates record it reads itself (``RATES_OBJECT``, with the provider's storage
  time) and the parent's administrative byte bound, and refuses any other policy;
* the stage's other input artifacts come from a registered producer for that
  operation; a stage whose operation is unmapped, ungranted, unpriced, unbounded or
  without a producer holds (``prepare`` returns None) before any intent is published.

The artifact set is published write once under its own digest, so the child can read the
exact bytes the context names.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from .brain_contract import canonical_bytes
from .daily_cost_policy import (
    MAX_RATES_BYTES,
    RATES_OBJECT,
    DailyCostPolicyRefusal,
    build_daily_cost_policy,
    validate_daily_cost_policy,
)
from .daily_cycle import validate_daily_profile
from .daily_execution_authority import derive_daily_execution
from .daily_execution_contracts import (
    DailyContractError,
    canonical_json_object,
    validate_artifact_set,
    validate_operation_context,
)
from .daily_operation_map import (
    DAILY_JOB_RESOURCE,
    DAILY_SERVICE_IDENTITY,
    ChildBinding,
    DailyOperationRefusal,
    child_binding,
    stage_operation,
)
from .daily_store import CONTROL_CONTRACT_V2, PreconditionFailed, child_job_resource_v2

ARTIFACT_SET_PREFIX = "42/daily/artifact-sets/"
OBSERVATION_PREFIX = "42/daily/execution-observations/"
CHILD_MANIFEST_CONTRACT = "daily_child_execution_manifest_v1"
RESERVED_ARTIFACTS = frozenset({"cost_policy", "daily_profile", "operation_context"})
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_APPROVAL_ID = re.compile(r"exa_[0-9a-f]{64}")
_DERIVATION_ID = re.compile(r"exd_[0-9a-f]{64}")
_EXECUTION = re.compile(re.escape(DAILY_JOB_RESOURCE) + r"/executions/[a-zA-Z0-9-]+")


class DailyDerivationRefusal(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise DailyDerivationRefusal(code)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> str:
    return canonical_bytes(value).decode("utf-8")


@dataclass(frozen=True, slots=True)
class ParentFacts:
    """The parent execution's own facts, each read from its native readback and build."""

    execution_name: str
    job_resource: str
    principal: str
    image_uri: str
    source_sha: str
    job_policy_digest: str


@dataclass(frozen=True, slots=True)
class AdmittedGrant:
    grant: Mapping[str, object]
    grant_digest: str
    approval_id: str


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def admit_grant_row(row: object) -> AdmittedGrant:
    """A row of ``sp_read_open_intelligence_recurring_grant_v2``, checked against itself."""
    if not isinstance(row, Mapping) or not isinstance(row.get("grant"), Mapping):
        _refuse("daily_grant_invalid")
    grant = row["grant"]
    digest = _sha256(canonical_bytes(grant))
    if (
        row.get("grant_sha256") != digest
        or not isinstance(row.get("approval_id"), str)
        or _APPROVAL_ID.fullmatch(row["approval_id"]) is None
        or grant.get("contract_version") != "42_recurring_execution_grant_v2"
        or grant.get("schema_version") != "42_daily_v2"
        or grant.get("environment") != "staging"
        or grant.get("source_estate_id") != "intelligence-42-core"
    ):
        _refuse("daily_grant_invalid")
    if row.get("state") != "active":
        _refuse("daily_grant_inactive")
    return AdmittedGrant(
        grant=_freeze(json.loads(canonical_bytes(grant))),
        grant_digest=digest,
        approval_id=row["approval_id"],
    )


def _parent_bound(parent: ParentFacts, grant: Mapping[str, object]) -> None:
    builds = [
        item
        for item in grant.get("build_provenance_bindings", ())
        if item.get("image_uri") == parent.image_uri
    ]
    jobs = [
        item
        for item in grant.get("job_policy_bindings", ())
        if item.get("job_resource") == DAILY_JOB_RESOURCE
    ]
    if (
        type(parent) is not ParentFacts
        or parent.job_resource != DAILY_JOB_RESOURCE
        or not isinstance(parent.execution_name, str)
        or _EXECUTION.fullmatch(parent.execution_name) is None
        or parent.principal != DAILY_SERVICE_IDENTITY
        or parent.principal not in grant.get("executing_principals", ())
        or not isinstance(parent.source_sha, str)
        or _GIT_SHA.fullmatch(parent.source_sha) is None
        or not isinstance(parent.job_policy_digest, str)
        or _HEX_64.fullmatch(parent.job_policy_digest) is None
        or len(builds) != 1
        or builds[0].get("source_sha") != parent.source_sha
        or not parent.image_uri.endswith("@sha256:" + str(builds[0].get("image_digest")))
        or len(jobs) != 1
        or jobs[0].get("job_policy_sha256") != parent.job_policy_digest
    ):
        _refuse("daily_parent_unbound")


class DailyDerivationAuthority:
    """The v2 kernel authority of one parent execution."""

    def __init__(
        self,
        *,
        grant_row: object,
        profile: object,
        parent: ParentFacts,
        registry,
        cost_policy: object,
        rates_objects,
        administrative_bytes_billed: int,
        producers: Mapping[str, Callable[..., Mapping[str, bytes]]],
        objects,
        store,
        write_clients,
        read_clients,
        now: Callable[[], datetime],
        binding_for: Callable[..., ChildBinding] = child_binding,
    ):
        self._grant = admit_grant_row(grant_row)
        grant = self._grant.grant
        try:
            checked = validate_daily_profile(profile)
        except ValueError as error:
            raise DailyDerivationRefusal("daily_profile_invalid") from error
        if (
            checked["schema_version"] != "42_daily_v2"
            or checked["recurring_grant_digest"] != self._grant.grant_digest
            or checked["resource_manifest_digest"] != grant.get("resource_manifest_digest")
            or checked["source_policy_digest"] != grant.get("source_policy_digest")
            or checked["source_estate_id"] != grant.get("source_estate_id")
        ):
            _refuse("daily_grant_profile_mismatch")
        _parent_bound(parent, grant)
        if getattr(store, "_owner", None) != parent.execution_name:
            _refuse("daily_parent_unbound")
        if not isinstance(cost_policy, Mapping) or (
            cost_policy.get("job_policy_digest") != parent.job_policy_digest
        ):
            _refuse("daily_cost_policy_job_mismatch")
        if not isinstance(producers, Mapping) or not all(
            callable(item) for item in producers.values()
        ):
            _refuse("daily_producers_invalid")
        if not callable(now) or not callable(binding_for):
            _refuse("daily_authority_invalid")
        self._profile = dict(checked)
        self._parent = parent
        self._registry = registry
        self._binding_for = binding_for
        self._cost_policy = self._recomputed_cost_policy(
            cost_policy, rates_objects, administrative_bytes_billed, now()
        )
        self._producers = dict(producers)
        self._objects = objects
        self._store = store
        self._write = write_clients
        self._read = read_clients
        self._now = now
        self._prepared: dict[str, bytes] = {}

    @property
    def read_clients(self):
        return self._read

    def _recomputed_cost_policy(self, policy, rates_objects, administrative_bytes_billed, now):
        """The caller's policy, only if it is exactly the one this authority computes."""
        raw, storage = rates_objects.read(RATES_OBJECT, MAX_RATES_BYTES)
        invalid = "daily_cost_rates_invalid"
        if (
            not isinstance(raw, bytes)
            or not isinstance(storage, Mapping)
            or storage.get("object_name") != RATES_OBJECT
            or storage.get("content_sha256") != _sha256(raw)
            or storage.get("size_bytes") != len(raw)
            or len(raw) > MAX_RATES_BYTES
        ):
            _refuse(invalid)
        try:
            record, _ = canonical_json_object(raw, invalid)
            stored_at = datetime.fromisoformat(storage.get("created_at"))
        except (DailyContractError, TypeError, ValueError) as error:
            raise DailyDerivationRefusal(invalid) from error
        reservations = policy.get("operation_reservations_micro_usd")
        if not isinstance(reservations, Mapping) or not reservations:
            _refuse("daily_cost_policy_mismatch")
        bindings = {}
        for operation in reservations:
            try:
                binding = self._binding_for(operation, registry=self._registry)
            except DailyOperationRefusal as error:
                raise DailyDerivationRefusal("daily_cost_policy_mismatch") from error
            bindings[operation] = binding
        try:
            expected = build_daily_cost_policy(
                rates=record,
                bindings=bindings,
                administrative_bytes_billed=administrative_bytes_billed,
                job_policy_digest=self._parent.job_policy_digest,
                now=now,
                stored_at=stored_at,
            )
        except DailyCostPolicyRefusal as error:
            if error.code == "daily_cost_policy_invalid":
                raise DailyDerivationRefusal("daily_cost_policy_mismatch") from error
            raise
        if canonical_bytes(expected) != canonical_bytes(policy):
            _refuse("daily_cost_policy_mismatch")
        return json.loads(canonical_bytes(expected))

    def _binding(self, operation: str) -> ChildBinding | None:
        try:
            binding = self._binding_for(operation, registry=self._registry)
        except DailyOperationRefusal:
            return None
        if (
            type(binding) is not ChildBinding
            or binding.operation != operation
            or binding.job_resource != child_job_resource_v2(binding.stage)
        ):
            _refuse("daily_child_binding_invalid")
        # A granted stage whose contract has no upper bound refuses rather than holds, so
        # it cannot pass for a stage that is merely not configured yet.
        binding.bounded_limits()
        return binding

    def _artifacts(self, operation, frame, binding) -> dict[str, bytes]:
        produced = self._producers[operation](frame=frame, binding=binding)
        if not isinstance(produced, Mapping):
            _refuse("daily_artifacts_invalid")
        artifacts = {}
        for name, body in produced.items():
            if not isinstance(name, str) or not name or name in RESERVED_ARTIFACTS:
                _refuse("daily_artifact_name_reserved")
            if not isinstance(body, bytes):
                _refuse("daily_artifacts_invalid")
            artifacts[name] = body
        artifacts["cost_policy"] = canonical_bytes(self._cost_policy)
        artifacts["daily_profile"] = canonical_bytes(self._profile)
        return artifacts

    def prepare(self, stage, frame, *, lease):
        operation = stage_operation(stage)
        grant = self._grant.grant
        if operation not in grant.get("allowed_operations", ()):
            return None
        binding = self._binding(operation)
        if binding is None or operation not in self._producers:
            return None
        policy = validate_daily_cost_policy(self._cost_policy, now=self._now())
        if operation not in policy["operation_reservations_micro_usd"]:
            return None
        if (
            not isinstance(lease, Mapping)
            or lease.get("owner") != self._parent.execution_name
            or type(lease.get("epoch")) is not int
            or lease["epoch"] < 1
        ):
            _refuse("daily_lease_invalid")
        if not isinstance(frame, Mapping) or frame.get("stage") != stage:
            _refuse("daily_frame_invalid")
        slot = frame["operation_id"]
        artifacts = self._artifacts(operation, frame, binding)
        envelope = {
            "contract_version": "daily_operation_artifact_set_v1",
            "artifacts": [
                {
                    "data": base64.b64encode(artifacts[name]).decode("ascii"),
                    "encoding": "base64",
                    "name": name,
                }
                for name in sorted(artifacts)
            ],
        }
        artifact_json = _json(envelope)
        validate_artifact_set(artifact_json)
        attempt = 1
        intent_id = "dsi_" + _sha256(
            canonical_bytes(
                {
                    "control_contract_version": CONTROL_CONTRACT_V2,
                    "slot_id": slot,
                    "stage": stage,
                    "attempt_version": attempt,
                }
            )
        )
        business_attempt_id = "bat_" + _sha256(
            canonical_bytes({"operation_id": slot, "attempt": f"{stage}@{attempt}"})
        )
        context = {
            "attempt_version": attempt,
            "authorizing_approval_id": self._grant.approval_id,
            "authorizing_grant_digest": self._grant.grant_digest,
            "business_attempt_id": business_attempt_id,
            "child_image_uri": self._parent.image_uri,
            "child_job_policy_digest": self._parent.job_policy_digest,
            "child_job_resource": binding.job_resource,
            "child_service_identity": binding.service_identity,
            "contract_version": "daily_operation_context_v1",
            "cutoff_utc": frame["cutoff_utc"],
            "execution_observation_prefix": OBSERVATION_PREFIX,
            "gcs_control_object": self._store._control_name(slot),
            "input_digest": _sha256(canonical_bytes(frame)),
            "intent_id": intent_id,
            "lease_epoch": lease["epoch"],
            "lease_owner": lease["owner"],
            "mode": "initial" if operation == "daily_source_snapshot_capture" else "run",
            "operation": operation,
            "operation_artifact_set_sha256": _sha256(artifact_json.encode("utf-8")),
            "parent_execution_name": self._parent.execution_name,
            "parent_job_resource": self._parent.job_resource,
            "parent_principal": self._parent.principal,
            "predecessor_result_digest": frame["predecessor_result_digest"],
            "predecessor_result_id": frame["predecessor_result_id"],
            "profile_digest": _sha256(canonical_bytes(self._profile)),
            "slot_id": slot,
            "source_estate_id": self._profile["source_estate_id"],
            "stage": stage,
        }
        context_json = _json(context)
        validate_operation_context(context_json)
        context_sha256 = _sha256(context_json.encode("utf-8"))
        manifest = {
            "contract_sha256": binding.contract_sha256,
            "contract_version": CHILD_MANIFEST_CONTRACT,
            "datasets": list(binding.datasets),
            "image_uri": self._parent.image_uri,
            "input_artifacts": sorted(
                [{"name": name, "sha256": _sha256(body)} for name, body in artifacts.items()]
                + [{"name": "operation_context", "sha256": context_sha256}],
                key=lambda item: item["name"],
            ),
            "job_resource": binding.job_resource,
            "limits": binding.bounded_limits(),
            "manifest_version": binding.manifest_version,
            "operation": operation,
            "registry_operation": binding.registry_operation,
            "service_identity": binding.service_identity,
            "source_sha": self._parent.source_sha,
        }
        manifest_json = _json(manifest)
        intent = {
            "intent_id": intent_id,
            "business_attempt_id": business_attempt_id,
            "stage": stage,
            "attempt_version": attempt,
            "input_digest": context["input_digest"],
            "operation_context_sha256": context_sha256,
            "authorizing_approval_id": self._grant.approval_id,
            "authorizing_grant_digest": self._grant.grant_digest,
            "child_job_resource": binding.job_resource,
            "lease_owner": lease["owner"],
            "lease_epoch": lease["epoch"],
            "phase": "prepared",
            "derivation_id": None,
            "dispatch_observation_reference": None,
            "resolution_reference": None,
        }
        prepared = {
            "intent": intent,
            "operation_context": context,
            "canonical_operation_context_json": context_json,
            "canonical_manifest_json": manifest_json,
            "manifest_sha256": _sha256(manifest_json.encode("utf-8")),
            "canonical_operation_artifact_set_json": artifact_json,
        }
        self._prepared[f"{stage}:{intent_id}"] = canonical_bytes(prepared)
        return prepared

    def _publish_artifact_set(self, context: Mapping[str, object], artifact_json: str) -> None:
        name = f"{ARTIFACT_SET_PREFIX}{context['operation_artifact_set_sha256']}.json"
        body = artifact_json.encode("utf-8")
        with contextlib.suppress(PreconditionFailed):
            self._objects.write(name, body, if_generation_match=0)
        found = self._objects.read(name)
        if found is None or found[0] != body:
            _refuse("daily_artifact_set_conflict")

    def issue(self, stage, prepared):
        intent = prepared.get("intent") if isinstance(prepared, Mapping) else None
        key = f"{stage}:{intent.get('intent_id')}" if isinstance(intent, Mapping) else None
        if key is None or self._prepared.get(key) != canonical_bytes(prepared):
            _refuse("daily_preparation_unknown")
        context = prepared["operation_context"]
        self._publish_artifact_set(context, prepared["canonical_operation_artifact_set_json"])
        row = derive_daily_execution(
            canonical_manifest_json=prepared["canonical_manifest_json"],
            manifest_sha256=prepared["manifest_sha256"],
            canonical_operation_context_json=prepared["canonical_operation_context_json"],
            operation_context_sha256=intent["operation_context_sha256"],
            canonical_operation_artifact_set_json=prepared["canonical_operation_artifact_set_json"],
            authorizing_grant_digest=self._grant.grant_digest,
            clients=self._write,
        )
        derivation_id = row.get("derivation_id")
        if (
            not isinstance(derivation_id, str)
            or _DERIVATION_ID.fullmatch(derivation_id) is None
            or row.get("authorizing_approval_id") != self._grant.approval_id
            or row.get("authorizing_approval_id") != context["authorizing_approval_id"]
            or row.get("authorizing_grant_digest") != self._grant.grant_digest
            or row.get("business_attempt_id") != intent["business_attempt_id"]
            or row.get("child_job_resource") != context["child_job_resource"]
            or row.get("manifest_sha256") != prepared["manifest_sha256"]
        ):
            _refuse("daily_derivation_readback_mismatch")
        return dict(row)


__all__ = [
    "ARTIFACT_SET_PREFIX",
    "CHILD_MANIFEST_CONTRACT",
    "AdmittedGrant",
    "DailyDerivationAuthority",
    "DailyDerivationRefusal",
    "ParentFacts",
    "admit_grant_row",
]
