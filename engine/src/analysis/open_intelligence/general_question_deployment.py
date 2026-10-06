"""Validate persisted worker bindings against separately observed runtime data."""

import copy
import re

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    require_digest,
    require_request_id,
)
from src.analysis.open_intelligence.general_question_policy import validate_question_policy

_SERVICE = "listening-post-staging"
_IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
_AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
_TASK_FIELDS = frozenset(
    {
        "contract_version",
        "request_id",
        "request_digest",
        "intake_digest",
        "policy_digest",
        "deployment_digest",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "service_name",
        "revision_name",
        "service_account_email",
        "lp_commit",
        "engine_bundle_digest",
        "canonical_service_audience",
        "sdk_version",
    }
)
_BINDING_FIELDS = _RUNTIME_FIELDS | {
    "contract_version",
    "project",
    "region",
    "image_digest",
    "worker_path",
    "policy_digest",
    "deployment_digest",
}


def _refuse():
    raise QuestionStoreError("approval_required")


def _runtime_fields(value):
    if value["service_name"] != _SERVICE or value["service_account_email"] != _IDENTITY:
        _refuse()
    revision = value["revision_name"]
    if (
        not isinstance(revision, str)
        or len(revision) > 63
        or not re.fullmatch(r"listening-post-staging-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", revision)
    ):
        _refuse()
    if not isinstance(value["lp_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", value["lp_commit"]
    ):
        _refuse()
    require_digest(value["engine_bundle_digest"], "approval_required")
    if not isinstance(value["sdk_version"], str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+){1,3}", value["sdk_version"]
    ):
        _refuse()
    if value["canonical_service_audience"] != _AUDIENCE:
        _refuse()


def validate_question_deployment(value):
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        _refuse()
    if (
        value["contract_version"] != "general_question_deployment_v1"
        or value["project"] != "ogilvy-trends-v2"
        or value["region"] != "us-central1"
        or value["worker_path"] != "/internal/general-question/execute"
    ):
        _refuse()
    _runtime_fields(value)
    for field in ("image_digest", "policy_digest", "deployment_digest"):
        require_digest(value[field], "approval_required")
    if value["deployment_digest"] != canonical_digest(
        {key: item for key, item in value.items() if key != "deployment_digest"}
    ):
        _refuse()
    return copy.deepcopy(value)


def validate_question_invocation(invocation, *, store, scope, runtime_identity):
    """Read bound context only; fresh call authority remains with the claimant."""
    if type(invocation) is not dict or set(invocation) != _TASK_FIELDS:
        raise QuestionStoreError("request_invalid")
    if invocation["contract_version"] != "general_cultural_question_v1":
        raise QuestionStoreError("request_invalid")
    require_request_id(invocation["request_id"])
    for field in _TASK_FIELDS - {"contract_version", "request_id"}:
        require_digest(invocation[field], "request_invalid")
    if type(runtime_identity) is not dict or set(runtime_identity) != _RUNTIME_FIELDS:
        _refuse()
    _runtime_fields(runtime_identity)
    try:
        supplied_policy = validate_question_policy(store.policy)
    except ValueError as error:
        raise QuestionStoreError("approval_required") from error
    if (
        invocation["deployment_digest"] != store.deployment_digest
        or invocation["policy_digest"] != supplied_policy["policy_digest"]
    ):
        _refuse()
    context = store.read_request(invocation["request_id"], scope=scope)
    if context is None:
        raise QuestionStoreError("request_unknown")
    _, control = store._control()
    admission = control["requests"].get(invocation["request_id"])
    if admission is None or any(
        admission[field] != context["admission"][field]
        for field in (
            "request_digest",
            "intake_digest",
            "request_generation",
            "intake_generation",
            "client_scope_id",
            "policy_digest",
            "deployment_digest",
            "admitted_at",
            "deadline_at",
        )
    ):
        raise QuestionStoreError("control_conflict")
    if (
        any(
            invocation[field] != admission[field]
            for field in ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
        )
        or control["bindings"].get(invocation["deployment_digest"]) != invocation["policy_digest"]
    ):
        _refuse()
    policy_object = store._objects.read(f"policies/{invocation['policy_digest']}/policy.json")
    deployment_object = store._objects.read(
        f"deployments/{invocation['deployment_digest']}/binding.json"
    )
    if policy_object is None or deployment_object is None:
        _refuse()
    try:
        stored_policy = validate_question_policy(policy_object.value)
    except ValueError as error:
        raise QuestionStoreError("approval_required") from error
    if stored_policy != supplied_policy:
        _refuse()
    binding = validate_question_deployment(deployment_object.value)
    if (
        binding["deployment_digest"] != invocation["deployment_digest"]
        or binding["policy_digest"] != invocation["policy_digest"]
        or any(binding[field] != runtime_identity[field] for field in _RUNTIME_FIELDS)
    ):
        _refuse()
    return {**context, "admission": copy.deepcopy(admission), "deployment": binding}
