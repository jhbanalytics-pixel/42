"""The reviewed meeting point of the daily derivation vocabulary and the origin registry.

The recurring grant v2 routines and the daily derivation routines name operations
``daily_*``; the origin registry binds operations to jobs, identities, datasets and
limits under its own names. Since amendment d the registry's daily row binds every
daily operation to the daily job ``intelligence-42-daily-staging`` under
``intelligence-42-orchestration``, so a daily child execution is an execution of that
job. This module is the only place the two vocabularies meet.

A mapping is admitted only where the daily operation fits the registry contract of the
operation it maps to. The daily contract revision gave collection and compose their own
registry operations: ``source_collection`` carries the run credit cap of 620 the grant
routine requires whenever collection is granted, which ``collection_exposure_issue``
pins to zero, and ``daily_composition_apply`` carries the daily compose's own bounds, so
the reviewed ``r3_apply`` rule of the R3 replay stays exactly as it was. No daily
operation is unmapped; ``UNMAPPED_OPERATIONS`` stays as the place a future one refuses
by name.

Limits come from the contract. An exact rule yields its value; a range yields its
maximum unless the maximum is the signed 64 bit sentinel the contracts use for "no
reviewed bound", which is reported as unbounded and never turned into a number here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .daily_execution_contracts import OPERATIONS
from .execution_origins import (
    ExecutionOrigin,
    OriginRefusal,
    OriginRegistry,
    _policy_bytes_for_origin,
    select_origin,
)

DAILY_JOB_RESOURCE = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
)
DAILY_SERVICE_IDENTITY = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
DAILY_REGISTRY_OPERATIONS = MappingProxyType(
    {
        "daily_source_collection": "source_collection",
        "daily_collection_exposure_issue": "collection_exposure_issue",
        "daily_source_snapshot_capture": "source_snapshot_capture",
        "daily_composition_apply": "daily_composition_apply",
        "daily_quality_proof_issue": "r3_proof_issue",
        "daily_staging_release": "r3_release",
    }
)
UNMAPPED_OPERATIONS: Mapping[str, str] = MappingProxyType({})
LIMIT_NAMES = ("max_bytes_billed", "max_credits", "max_model_calls", "max_rows_written")
_UNBOUNDED = 9223372036854775807
_MODE = "new_consume"


class DailyOperationRefusal(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise DailyOperationRefusal(code)


@dataclass(frozen=True, slots=True)
class ChildBinding:
    operation: str
    stage: str
    registry_operation: str
    job_resource: str
    service_identity: str
    datasets: tuple[str, ...]
    manifest_version: str
    contract_sha256: str
    limit_rules: Mapping[str, Mapping[str, int]]

    def unbounded_limits(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in LIMIT_NAMES
            if "exact" not in self.limit_rules[name]
            and self.limit_rules[name]["maximum"] >= _UNBOUNDED
        )

    def bounded_limits(self) -> dict[str, int]:
        if self.unbounded_limits():
            _refuse("daily_limit_unbounded")
        return {
            name: self.limit_rules[name].get("exact", self.limit_rules[name].get("maximum"))
            for name in LIMIT_NAMES
        }


def _origin_for(operation: str, *, registry: OriginRegistry) -> ExecutionOrigin:
    matches = [
        origin
        for origin in registry.values()
        if origin.manifest_version == "open_intelligence_execution_manifest_v2"
        and operation in origin.operation_bindings
    ]
    if len(matches) != 1:
        _refuse("daily_registry_binding_ambiguous")
    origin = matches[0]
    return select_origin(
        manifest_version=origin.manifest_version,
        contract_sha256=origin.contract_sha256,
        mode=_MODE,
        registry=registry,
    )


def _policy_bytes(origin: ExecutionOrigin, registry: OriginRegistry) -> bytes:
    return _policy_bytes_for_origin(registry=registry, origin=origin)


def _limit_rules(rule: object) -> Mapping[str, Mapping[str, int]]:
    if not isinstance(rule, Mapping) or set(rule) != set(LIMIT_NAMES):
        _refuse("daily_contract_limits_invalid")
    checked = {}
    for name in LIMIT_NAMES:
        entry = rule[name]
        if not isinstance(entry, Mapping) or set(entry) not in ({"exact"}, {"minimum", "maximum"}):
            _refuse("daily_contract_limits_invalid")
        if any(type(value) is not int or value < 0 for value in entry.values()):
            _refuse("daily_contract_limits_invalid")
        checked[name] = MappingProxyType(dict(entry))
    return MappingProxyType(checked)


def child_binding(operation: object, *, registry: OriginRegistry) -> ChildBinding:
    """The registry facts a daily child execution of ``operation`` runs under."""
    if isinstance(operation, str) and operation in UNMAPPED_OPERATIONS:
        _refuse(UNMAPPED_OPERATIONS[operation])
    if not isinstance(operation, str) or operation not in DAILY_REGISTRY_OPERATIONS:
        _refuse("daily_operation_unmapped")
    if type(registry) is not OriginRegistry:
        _refuse("daily_registry_invalid")
    registry_operation = DAILY_REGISTRY_OPERATIONS[operation]
    try:
        origin = _origin_for(registry_operation, registry=registry)
    except OriginRefusal as error:
        raise DailyOperationRefusal("daily_registry_binding_unavailable") from error
    binding = origin.operation_bindings[registry_operation]
    if (
        binding.job_resource != DAILY_JOB_RESOURCE
        or binding.service_identity != DAILY_SERVICE_IDENTITY
    ):
        _refuse("daily_child_job_unbound")
    policy = json.loads(_policy_bytes(origin, registry))
    rule = policy.get("operation_validation", {}).get(registry_operation)
    if not isinstance(rule, Mapping):
        _refuse("daily_contract_rule_missing")
    if (
        rule.get("job_resource") != DAILY_JOB_RESOURCE
        or rule.get("service_identity") != DAILY_SERVICE_IDENTITY
        or list(rule.get("datasets", ())) != list(binding.datasets)
    ):
        _refuse("daily_contract_binding_mismatch")
    return ChildBinding(
        operation=operation,
        stage=OPERATIONS[operation],
        registry_operation=registry_operation,
        job_resource=binding.job_resource,
        service_identity=binding.service_identity,
        datasets=tuple(binding.datasets),
        manifest_version=origin.manifest_version,
        contract_sha256=origin.contract_sha256,
        limit_rules=_limit_rules(rule.get("limits")),
    )


def stage_operation(stage: object) -> str:
    """The daily operation a v2 kernel stage runs."""
    for operation, name in OPERATIONS.items():
        if name == stage:
            return operation
    _refuse("daily_stage_unknown")
    raise AssertionError  # unreachable


__all__ = [
    "DAILY_JOB_RESOURCE",
    "DAILY_REGISTRY_OPERATIONS",
    "DAILY_SERVICE_IDENTITY",
    "LIMIT_NAMES",
    "UNMAPPED_OPERATIONS",
    "ChildBinding",
    "DailyOperationRefusal",
    "child_binding",
    "stage_operation",
]
