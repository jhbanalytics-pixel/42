"""Admit a normalized question through the bound staging store."""

from __future__ import annotations

from time import monotonic
from uuid import UUID

from src.analysis.open_intelligence.client_lens import (
    CLIENT_LENS_BINDING_VERSION,
    ClientLensUnavailable,
    resolve_client_lens,
)
from src.analysis.open_intelligence.client_overlay import (
    ClientPurposeProhibited,
    refuse_prohibited_client_purpose,
)
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    require_digest,
    require_request_id,
    utc_time,
)
from src.analysis.open_intelligence.general_question_deployment import (
    _RUNTIME_FIELDS,
    validate_question_deployment,
)
from src.analysis.open_intelligence.general_question_parent_context import admission_stage
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
    require_question_admission_policy,
    validate_intake_context,
    validate_question_policy,
)
from src.analysis.open_intelligence.general_question_request import (
    normalize_question_request,
    validate_question_request,
)

_FIELDS = {
    "contract_version",
    "request_id",
    "transport",
    "scope",
    "selected_market",
    "policy_digest",
    "deployment_digest",
}
# The optional client lens the caller names. It is additive on both admission
# versions, and it asserts a lens rather than granting one: the two values are
# re-resolved here against the configuration bytes this engine reads.
_LENS_FIELDS = {"lens_binding_version", "client_lens_id", "configuration_digest"}


def _invalid():
    raise QuestionStoreError("request_invalid")


def _validate_input(value):
    v2 = type(value) is dict and value.get("contract_version") == "general_question_admission_v2"
    required = _FIELDS | ({"parent_request_id", "thread_anchor_request_id"} if v2 else set())
    if type(value) is not dict or not required <= set(value) <= required | {"client_lens"}:
        _invalid()
    if "client_lens" in value:
        lens = value["client_lens"]
        if type(lens) is not dict or set(lens) != _LENS_FIELDS:
            _invalid()
        if lens["lens_binding_version"] != CLIENT_LENS_BINDING_VERSION:
            _invalid()
        if type(lens["client_lens_id"]) is not str or not lens["client_lens_id"]:
            _invalid()
        require_digest(lens["configuration_digest"], "request_invalid")
    if value["contract_version"] != (
        "general_question_admission_v2" if v2 else "general_question_admission_v1"
    ):
        _invalid()
    require_request_id(value["request_id"])
    require_digest(value["policy_digest"], "request_invalid")
    require_digest(value["deployment_digest"], "request_invalid")
    if type(value["transport"]) is not dict or type(value["scope"]) is not dict:
        _invalid()
    if value["selected_market"] is not None and type(value["selected_market"]) is not str:
        _invalid()
    if v2:
        try:
            require_request_id(value["parent_request_id"])
            if value["thread_anchor_request_id"] is not None:
                require_request_id(value["thread_anchor_request_id"])
            if value["request_id"] in (
                value["parent_request_id"],
                value["thread_anchor_request_id"],
            ):
                raise ValueError()
        except (ValueError, QuestionStoreError) as error:
            raise QuestionStoreError("parent_reference_invalid") from error
    return value


def _validate_authority(value, store, runtime_identity):
    if (
        value["policy_digest"] != store.policy["policy_digest"]
        or value["deployment_digest"] != store.deployment_digest
        or type(runtime_identity) is not dict
        or set(runtime_identity) != _RUNTIME_FIELDS
    ):
        raise QuestionStoreError("approval_required")
    policy_object = store._objects.read(f"policies/{value['policy_digest']}/policy.json")
    deployment_object = store._objects.read(
        f"deployments/{value['deployment_digest']}/binding.json"
    )
    if policy_object is None or deployment_object is None:
        raise QuestionStoreError("approval_required")
    try:
        policy = validate_question_policy(policy_object.value)
        binding = validate_question_deployment(deployment_object.value)
    except ValueError as exc:
        raise QuestionStoreError("approval_required") from exc
    if (
        policy != store.policy
        or binding["policy_digest"] != value["policy_digest"]
        or binding["deployment_digest"] != value["deployment_digest"]
        or any(binding[field] != runtime_identity[field] for field in _RUNTIME_FIELDS)
    ):
        raise QuestionStoreError("approval_required")


def _validate_client_lens(value):
    """Resolve the named lens against this scope and these bytes, or refuse it.

    General 42 names no lens. A named one is authorized by the registry entry for
    the caller's own client scope and brand configuration, and the digest it
    carries must be the digest of the configuration document this engine reads,
    so a moved document and another client's lens are refused before any work.
    """
    envelope = value.get("client_lens")
    scope = value["scope"]
    if envelope is None:
        if not isinstance(scope, dict):
            raise QuestionStoreError("scope_invalid")
        return None
    try:
        lens = resolve_client_lens(
            client_scope_id=scope.get("client_scope_id"),
            brand_config_id=scope.get("brand_config_id"),
            client_lens_id=envelope["client_lens_id"],
        )
    except (ClientLensUnavailable, AttributeError) as error:
        raise QuestionStoreError("scope_invalid") from error
    if lens is None or lens.configuration_digest != envelope["configuration_digest"]:
        raise QuestionStoreError("scope_invalid")
    return lens


def _admitted_at(value, store):
    existing = store.read_request(value["request_id"], scope=value["scope"])
    if existing is not None:
        return control_time(existing["request"]["as_of"]), existing["intake"], True
    orphan = store._objects.read(f"requests/{value['request_id']}/request.json")
    if orphan is None:
        return None, None, False
    try:
        request = validate_question_request(
            orphan.value,
            scope=value["scope"],
            policy_digest=value["policy_digest"],
        )
    except ValueError as exc:
        raise QuestionStoreError("run_id_conflict") from exc
    intake = store._objects.read(f"requests/{value['request_id']}/intake.json")
    return (
        control_time(request["as_of"]),
        validate_intake_context(intake.value, request=request) if intake is not None else None,
        False,
    )


def _require_current_policy(store, now):
    """Refuse a new reservation under a policy whose pricing review has lapsed.

    The store makes the same check inside the reservation, where it cannot tell a
    lapsed review from a request that ran out of time and reports both as
    request_expired. A lapsed review or an expired policy is an approval that no
    longer holds, and no amount of retrying the question changes it; only a
    pricing renewal does. So it is refused here, as approval_required, before the
    intake is built or anything is written. An already reserved request is not
    re-reserved and stays readable, so a retry of one is not refused here.
    """
    try:
        require_question_admission_policy(store.policy, now=now)
    except ValueError as exc:
        raise QuestionStoreError("approval_required") from exc


def admit_question_transport(value, *, store, runtime_identity, now, diagnostics=None):
    value = _validate_input(value)
    if value["contract_version"] == "general_question_admission_v2":
        from src.analysis.open_intelligence.general_question_parent_context import (
            parent_read_attempt,
        )

        with (
            parent_read_attempt(store, deadline=monotonic() + 20),
            admission_stage("admission", request_id=value["request_id"], diagnostics=diagnostics),
        ):
            return _admit_question_transport(
                value, store=store, runtime_identity=runtime_identity, now=now
            )
    with admission_stage("admission", request_id=value["request_id"], diagnostics=diagnostics):
        return _admit_question_transport(
            value, store=store, runtime_identity=runtime_identity, now=now
        )


def _admit_question_transport(value, *, store, runtime_identity, now):
    now = utc_time(now)
    with admission_stage("authority"):
        _validate_authority(value, store, runtime_identity)
        # The resolved lens is carried, not discarded: it joins the request record
        # so the same wording admitted with and without it is two durable requests,
        # and it is what selects the client configuration further down.
        lens = _validate_client_lens(value)
    with admission_stage("child_lookup"):
        admitted_at, retained_intake, reserved = _admitted_at(value, store)
    if not reserved:
        with admission_stage("policy_freshness"):
            _require_current_policy(store, now)
    admitted_at = admitted_at or now
    try:
        request = normalize_question_request(
            value["transport"],
            scope=value["scope"],
            request_id=value["request_id"],
            admitted_at=admitted_at,
            policy_digest=value["policy_digest"],
            client_lens=None if lens is None else value["client_lens"],
        )
        if value["contract_version"] == "general_question_admission_v2":
            from src.analysis.open_intelligence.general_question_parent_context import (
                prepare_parent_intake,
            )

            with admission_stage("parent_intake"):
                intake = prepare_parent_intake(
                    store,
                    request,
                    scope=value["scope"],
                    parent_id=value["parent_request_id"],
                    anchor_id=value["thread_anchor_request_id"],
                    selected_market=value["selected_market"],
                    retained_intake=retained_intake,
                )
        elif retained_intake is not None:
            with admission_stage("intake_context"):
                intake = validate_intake_context(retained_intake, request=request)
            if (
                "parent_context_ref" in intake
                or intake["selected_market"] != value["selected_market"]
            ):
                raise QuestionStoreError("run_id_conflict")
        else:
            with admission_stage("intake_context"):
                intake = build_intake_context(
                    request, selected_market=value["selected_market"], source_window=True
                )
    except ValueError as exc:
        raise QuestionStoreError("request_invalid") from exc
    # The client purpose policy runs before any reservation; general 42 names no
    # overlay, so the same wording under the default scope follows the general policy.
    # A named lens is what selects the configuration here; a scope that names none
    # reaches only the entry its own client scope authorizes.
    try:
        refuse_prohibited_client_purpose(
            value["scope"], [request["question"]], stage="admission", lens=lens
        )
    except ClientPurposeProhibited as exc:
        raise QuestionStoreError(exc.code) from exc
    except ValueError as exc:
        raise QuestionStoreError("scope_invalid") from exc
    with admission_stage("reservation"):
        admission = store.admit(request, intake, scope=value["scope"], now=now)
    invocation = {
        "contract_version": "general_cultural_question_v1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "policy_digest": value["policy_digest"],
        "deployment_digest": value["deployment_digest"],
    }
    return {
        "contract_version": "general_question_admission_result_v1",
        "job_id": f"chat_{UUID(request['request_id']).hex}",
        "invocation": invocation,
        "request_generation": admission["request_generation"],
        "intake_generation": admission["intake_generation"],
        "deadline_at": admission["deadline_at"],
    }


__all__ = ["admit_question_transport"]
