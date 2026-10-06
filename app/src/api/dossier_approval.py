"""The dossier approval state machine, and its refusal to run.

Approval requires a server-authenticated principal and an immutable create-only
record. The passcode provides neither: it proves someone knows a shared string,
not who they are, and it leaves nothing behind that cannot be rewritten. Until
identity validation, role mapping and durable storage are separately approved
and proved, every approval action refuses with one code and every control is
disabled with that same reason.

The state machine is built and exercised anyway. A refusal has to be a refusal
to perform a known transition, naming the role it would have required and the
state it would have reached. A system that cannot say what it would have done is
not withholding an action, it simply lacks one, and the difference matters to
whoever reads the refusal.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

CONTRACT_VERSION = "intelligence_dossier_v1"
REVIEW_CONTRACT_VERSION = "dossier_review_v1"

# The four roles, and no others. One human may hold more than one, but only by
# being separately allowlisted, which is a decision recorded elsewhere.
APPROVAL_ROLES = frozenset(
    {
        "dossier_editor",
        "claim_approver",
        "relationship_approver",
        "client_read_approver",
    }
)

AUTHORITY_UNAVAILABLE = "approval_authority_unavailable"

# Exactly the transitions the contract defines, keyed by what is being asked.
# Nothing outside this table exists, which is what makes "no transition reverses
# in place" enforceable rather than merely stated: the reverse is simply absent.
_TRANSITIONS = MappingProxyType(
    {
        ("claim", "pending_review", "approve"): ("claim_approver", "approved"),
        ("claim", "pending_review", "reject"): ("claim_approver", "rejected"),
        ("relationship", "pending_review", "approve"): (
            "relationship_approver",
            "approved",
        ),
        ("relationship", "pending_review", "reject"): (
            "relationship_approver",
            "rejected",
        ),
        ("artifact", "draft", "submit"): ("dossier_editor", "pending_review"),
        ("artifact", "pending_review", "approve"): ("client_read_approver", "approved"),
        ("artifact", "pending_review", "reject"): ("client_read_approver", "rejected"),
    }
)

# A pseudonymous server identity. Raw email, token or identity-provider claims
# never enter a dossier or Client Read payload, so the reference carries none.
_PRINCIPAL_REF = re.compile(r"\Ahuman:[0-9a-f]{8}\Z")


class ApprovalTransitionUnknown(ValueError):
    """The contract defines no such transition."""


class ApprovalPrincipalInvalid(ValueError):
    """The principal cannot hold an approval role."""


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    resource: str
    current_state: str
    action: str
    required_role: str
    next_state: str
    # Named to be read. Approving an artifact never approves its claims or
    # relationships, and approving a claim never approves a relationship, so
    # this is always empty and says so rather than being absent.
    approves_by_implication: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorityState:
    available: bool
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class Refusal:
    status: int
    code: str
    reason: str
    required_role: str
    would_have_reached: str


@dataclass(frozen=True, slots=True)
class ControlState:
    enabled: bool
    code: str
    reason: str


def plan_transition(
    resource: object, current_state: object, action: object
) -> TransitionPlan:
    """What the contract says this transition requires and reaches."""
    key = (resource, current_state, action)
    entry = _TRANSITIONS.get(key)  # type: ignore[arg-type]
    if entry is None:
        raise ApprovalTransitionUnknown(
            f"no approved transition for {resource!r} in {current_state!r} on {action!r}"
        )
    required_role, next_state = entry
    return TransitionPlan(
        resource=str(resource),
        current_state=str(current_state),
        action=str(action),
        required_role=required_role,
        next_state=next_state,
    )


def initial_state(resource: object) -> str:
    """The state a resource starts in, read off the table rather than declared.

    It is the one state the table leaves for that resource and never reaches.
    A resource the table does not know has no initial state, and a table that
    gave a resource two starting points would be a defect, so both are refused
    rather than guessed.
    """
    left = {state for kind, state, _action in _TRANSITIONS if kind == resource}
    reached = {
        next_state
        for (kind, _state, _action), (_role, next_state) in _TRANSITIONS.items()
        if kind == resource
    }
    origins = left - reached
    if len(origins) != 1:
        raise ApprovalTransitionUnknown(f"no initial state for {resource!r}")
    return next(iter(origins))


def authority_state() -> AuthorityState:
    """Whether an approval can be performed at all.

    Unavailable, and it will stay unavailable until identity validation, role
    mapping and immutable storage are separately approved and proved. It is a
    single fact, so the action and the control read it from the same place and
    cannot disagree.
    """
    return AuthorityState(
        available=False,
        code=AUTHORITY_UNAVAILABLE,
        reason=(
            "Approval needs a server-authenticated identity and a record that "
            "cannot be rewritten. Neither exists yet, so no approval can be made."
        ),
    )


def attempt_transition(
    resource: object, current_state: object, action: object, *, principal_ref: object
) -> Refusal:
    """Refuse a known transition, rather than deny that it exists.

    The transition is resolved first and on purpose. An action the contract does
    not define stays undefined: authority being unavailable must never turn an
    impossible action into a merely deferred one.
    """
    plan = plan_transition(resource, current_state, action)
    authority = authority_state()
    if not authority.available:
        return Refusal(
            status=409,
            code=authority.code,
            reason=authority.reason,
            required_role=plan.required_role,
            would_have_reached=plan.next_state,
        )
    # Unreachable while authority_state is fixed unavailable. It is written as a
    # raise rather than a fall-through so that granting authority cannot quietly
    # begin approving without the record and identity work that comes with it.
    raise ApprovalTransitionUnknown("approval execution is not implemented")


def control_state(
    resource: object,
    current_state: object,
    action: object,
    *,
    authority: AuthorityState | None = None,
) -> ControlState:
    """Whether the control for this transition may be offered.

    Disabled for the same reason the action refuses, read from the same place,
    so a control can never invite an action that will be refused. A caller that
    holds an admitted authority passes the same object it will record with.
    """
    plan_transition(resource, current_state, action)
    if authority is None:
        authority = authority_state()
    return ControlState(
        enabled=authority.available,
        code=authority.code,
        reason=authority.reason,
    )


def validate_principal(principal_ref: object) -> str:
    """Only a pseudonymous reference to an authenticated human holds a role.

    Refused by shape rather than by naming what is forbidden. A service account,
    a model, the passcode, a browser field and a stored legacy author all fail
    the same way, and so does anything invented later, because none of them can
    produce this reference.
    """
    if not isinstance(principal_ref, str) or not _PRINCIPAL_REF.fullmatch(
        principal_ref
    ):
        raise ApprovalPrincipalInvalid("principal cannot hold an approval role")
    return principal_ref


class ReviewRefused(Exception):
    """A review command refused with the package status and code, as a Refusal."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(refusal.code)
        self.refusal = refusal


def _refuse_review(
    status: int, code: str, reason: str, plan: TransitionPlan | None = None
) -> None:
    raise ReviewRefused(
        Refusal(
            status=status,
            code=code,
            reason=reason,
            required_role=plan.required_role if plan is not None else "",
            would_have_reached=plan.next_state if plan is not None else "",
        )
    )


_COMMAND_IDENTITY = (
    "dossier_version",
    "resource",
    "resource_id",
    "resource_version",
    "action",
    "expected_state",
    "support_review",
)


def _recorded_at(now: datetime | None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TypeError("now must be an aware datetime")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _review_result(decision: Mapping[str, object]) -> dict:
    return {
        "contract_version": REVIEW_CONTRACT_VERSION,
        "investigation_id": decision["investigation_id"],
        "dossier_version": decision["dossier_version"],
        "decision_id": decision["decision_id"],
        "state": decision["next_state"],
    }


def record_review(
    *,
    bucket: object,
    scope: object,
    principal: object,
    command: object,
    authority: AuthorityState | None = None,
    now: datetime | None = None,
) -> dict:
    """Record one human decision on one exact resource of the current version.

    The principal is the server's, resolved from the validated credential, and
    the command never names one. Every check runs before anything is written:
    the principal holds a contract role, the command has the package shape, the
    scope names a real investigation, the version named is the scope's current
    version, the resource is in the state the version's state pointer holds and
    carries the content digest the reviewer saw, the transition exists, the role
    is the one the transition requires, and the authority is admitted. The
    decision is stored create-only and then committed by moving that pointer at
    the generation read; a writer whose move loses is refused and its decision
    stays as an unapplied record, never a state. authority is the
    admitted authority the route supplies once identity, roles and storage have
    been separately approved; without it the module's own authority state
    applies, which is unavailable, so a bare call refuses and writes nothing.

    A retry carrying the same idempotency key and the same command from the
    same principal returns the decision already recorded. The same key with a
    different command is a conflict.
    """
    # The store imports this module for the transition grammar, so it is
    # imported here rather than at module level.
    from . import dossier_review_store, dossier_store

    prefix = dossier_store.STAGING_PREFIX
    if not isinstance(principal, Mapping):
        _refuse_review(403, "review_role_required", "no principal holds a role")
    try:
        principal_ref = validate_principal(principal.get("principal_ref"))
    except ApprovalPrincipalInvalid:
        _refuse_review(403, "review_role_required", "principal cannot hold a role")
    role = principal.get("role")
    if role not in APPROVAL_ROLES:
        _refuse_review(403, "review_role_required", "principal holds no contract role")
    # A principal may hold several roles; one that names none holds only its role.
    roles = principal.get("roles", [role])
    if (
        type(roles) is not list
        or role not in roles
        or len(set(roles)) != len(roles)
        or not all(item in APPROVAL_ROLES for item in roles)
    ):
        _refuse_review(403, "review_role_required", "principal holds no contract role")
    try:
        command = dossier_review_store.validate_review_command(command)
    except dossier_store.DossierRecordInvalid:
        _refuse_review(400, "review_request_invalid", "review command is invalid")
    investigation_id = getattr(scope, "investigation_id", None)
    scope_digest = getattr(scope, "scope_digest", None)
    try:
        dossier_store._validate_investigation_id(investigation_id)
    except ValueError:
        _refuse_review(404, "scope_invalid", "scope is unavailable")
    if not dossier_store._digest(scope_digest):
        _refuse_review(404, "scope_invalid", "scope is unavailable")
    resource = command["resource"]
    resource_id = command["resource_id"]
    try:
        current = dossier_store.read_pointer(
            bucket=bucket, prefix=prefix, investigation_id=investigation_id
        )
    except dossier_store.DossierStoreError:
        _refuse_review(503, "review_storage_unavailable", "review storage failed")
    if current is None:
        _refuse_review(409, "review_version_conflict", "no dossier version is current")
    pointer = current["pointer"]
    if pointer["scope_digest"] != scope_digest:
        _refuse_review(404, "scope_invalid", "scope is unavailable")
    if pointer["dossier_version"] != command["dossier_version"]:
        _refuse_review(409, "review_version_conflict", "dossier version is not current")
    try:
        decisions = dossier_review_store.list_decisions(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=command["dossier_version"],
        )
        current = dossier_review_store.read_state_pointer(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=command["dossier_version"],
        )
    except dossier_store.DossierStoreError:
        _refuse_review(503, "review_storage_unavailable", "review storage failed")
    history = dossier_review_store.resource_pointer_state(
        current["pointer"], resource=resource, resource_id=resource_id
    )
    for decision in decisions:
        if (
            decision["idempotency_key"] != command["idempotency_key"]
            or decision["principal_ref"] != principal_ref
        ):
            continue
        if all(decision[field] == command[field] for field in _COMMAND_IDENTITY):
            return _commit_decision(bucket, prefix, decision, current, None)
        _refuse_review(
            409, "review_version_conflict", "idempotency key names another command"
        )
    plan = plan_transition(resource, command["expected_state"], command["action"])
    if history["state"] != command["expected_state"]:
        _refuse_review(409, "review_version_conflict", "resource state has moved", plan)
    if (
        history["resource_version"] is not None
        and history["resource_version"] != command["resource_version"]
    ):
        _refuse_review(
            409, "review_version_conflict", "resource version is not current", plan
        )
    if plan.required_role not in roles:
        _refuse_review(
            403, "review_role_required", "role cannot take this action", plan
        )
    if authority is None:
        authority = authority_state()
    control = control_state(
        resource, history["state"], command["action"], authority=authority
    )
    if not authority.available or not control.enabled:
        _refuse_review(503, "review_authority_unavailable", authority.reason, plan)
    decision = dossier_review_store.build_decision(
        investigation_id=investigation_id,
        dossier_version=command["dossier_version"],
        resource=resource,
        resource_id=resource_id,
        resource_version=command["resource_version"],
        action=command["action"],
        expected_state=command["expected_state"],
        idempotency_key=command["idempotency_key"],
        support_review=command["support_review"],
        principal_ref=principal_ref,
        recorded_at=_recorded_at(now),
        acting_role=plan.required_role,
    )
    try:
        written = dossier_review_store.create_decision(
            bucket=bucket, prefix=prefix, decision=decision
        )
    except dossier_store.DossierConflict:
        _refuse_review(
            409,
            "review_version_conflict",
            "decision already recorded differently",
            plan,
        )
    except dossier_store.DossierStoreError:
        _refuse_review(503, "review_storage_unavailable", "review storage failed", plan)
    return _commit_decision(bucket, prefix, written["decision"], current, plan)


def _commit_decision(
    bucket: object,
    prefix: str,
    decision: Mapping[str, object],
    current: Mapping[str, object],
    plan: TransitionPlan | None,
) -> dict:
    """Move the state pointer to a stored decision, or refuse and leave it unapplied.

    A pointer that moved under this writer is two different refusals with one
    code: when the resource is still where the decision expects it the reason
    says so and the same command can be retried; when the resource moved, the
    reason says that, and the decision is history.
    """
    from . import dossier_review_store, dossier_store

    try:
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=prefix, decision=decision, current=current
        )
    except dossier_store.DossierConflict as error:
        if error.code == "state_pointer_generation_moved":
            _refuse_review(
                409,
                "review_version_conflict",
                "state pointer moved under this command; retry it",
                plan,
            )
        _refuse_review(409, "review_version_conflict", "resource state has moved", plan)
    except dossier_store.DossierStoreError:
        _refuse_review(503, "review_storage_unavailable", "review storage failed", plan)
    return _review_result(decision)
