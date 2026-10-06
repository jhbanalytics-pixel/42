"""Create-only storage for dossier review decisions, and the state they replay to.

A decision is one immutable record about one resource inside one exact dossier
version: which transition was asked for, from which state, by which pseudonymous
human, on which exact content digest, at which instant. Its id is the canonical
digest of everything else in it, and the object name carries that id, so one
name can only ever hold one byte sequence. The writer creates with a zero
generation precondition and never overwrites: an identical retry is the same
decision, a different record under the same name is a conflict.

The committed state of every resource in one dossier version lives in one
state pointer beside the decisions, an append-only log of numbered entries
that moves by creating the next entry after the decision object has landed. A decision is history the moment it is stored and
becomes the resource's state only when the pointer move that names it wins.
The replay over every stored decision remains available as a view, but a
decision the pointer never applied is an unapplied record, not a state.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from itertools import groupby

from google.api_core.exceptions import PreconditionFailed

from . import dossier_approval, dossier_store
from .dossier_store import (
    STAGING_PREFIX,
    DossierConflict,
    DossierRecordInvalid,
    DossierStoreError,
    canonical_digest,
)

__all__ = [
    "COMMAND_FIELDS",
    "DECISION_FIELDS",
    "RESOURCES",
    "REVIEW_CONTRACT_VERSION",
    "STAGING_PREFIX",
    "SUPPORT_REVIEW_FIELDS",
    "SUPPORT_VERDICTS",
    "build_decision",
    "canonical_bytes",
    "create_decision",
    "decision_directory",
    "decision_object_name",
    "list_decisions",
    "parse_decision",
    "read_decision",
    "replay_state",
    "resource_state",
    "validate_review_command",
]

REVIEW_CONTRACT_VERSION = "dossier_review_v1"
RESOURCES = frozenset({"claim", "relationship", "artifact"})
SUPPORT_VERDICTS = frozenset({"supported", "partial", "contradictory", "unsupported"})
SUPPORT_REVIEW_FIELDS = ("verdict", "receipt_ids", "note")
COMMAND_FIELDS = (
    "contract_version",
    "dossier_version",
    "resource",
    "resource_id",
    "resource_version",
    "action",
    "expected_state",
    "idempotency_key",
    "support_review",
)
DECISION_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "resource",
    "resource_id",
    "resource_version",
    "action",
    "expected_state",
    "next_state",
    # The reached state under the name the projection reads. It always equals
    # next_state, and a record where the two differ is refused.
    "state",
    "idempotency_key",
    "support_review",
    "principal_ref",
    "recorded_at",
    "decision_id",
)
# A decision made by a subject that holds more than one role names the role it
# acted in, which is always the role its transition requires, so two roles
# held by one person stay distinct in the record. Decisions recorded before
# this field existed keep the shape above and remain readable.
ACTED_DECISION_FIELDS = (*DECISION_FIELDS[:-1], "acting_role", DECISION_FIELDS[-1])

_DECISION_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_TEXT_LIMIT = 256


def canonical_bytes(value: object) -> bytes:
    """The one serialization of a record, the same rule the dossier store uses."""
    return dossier_store._canonical(value)


def _invalid(code: str = "decision_record_invalid") -> None:
    raise DossierRecordInvalid(code)


def _bounded_text(value: object) -> bool:
    return (
        dossier_store._text(value)
        and len(value) <= _TEXT_LIMIT
        and value.isascii()
        and value.isprintable()
    )


def _validate_support_review(resource: str, review: object) -> None:
    if resource != "claim":
        if review is not None:
            _invalid()
        return
    if type(review) is not dict or set(review) != set(SUPPORT_REVIEW_FIELDS):
        _invalid()
    receipts = review["receipt_ids"]
    if (
        type(review["verdict"]) is not str
        or review["verdict"] not in SUPPORT_VERDICTS
        or type(receipts) is not list
        or not all(_bounded_text(receipt) for receipt in receipts)
        or len(set(receipts)) != len(receipts)
        or type(review["note"]) is not str
    ):
        _invalid()


def _validate_transition_fields(fields: Mapping[str, object]) -> None:
    """The fields a command and a decision share, refused by shape."""
    if (
        not dossier_store._digest(fields["dossier_version"])
        or fields["resource"] not in RESOURCES
        or not _bounded_text(fields["resource_id"])
        or not dossier_store._digest(fields["resource_version"])
        or not _bounded_text(fields["action"])
        or not _bounded_text(fields["expected_state"])
        or not _bounded_text(fields["idempotency_key"])
    ):
        _invalid()
    _validate_support_review(fields["resource"], fields["support_review"])


def _plan(fields: Mapping[str, object]) -> dossier_approval.TransitionPlan:
    try:
        return dossier_approval.plan_transition(
            fields["resource"], fields["expected_state"], fields["action"]
        )
    except dossier_approval.ApprovalTransitionUnknown:
        _invalid()


def _validate_core(fields: Mapping[str, object]) -> dossier_approval.TransitionPlan:
    try:
        dossier_store._validate_investigation_id(fields["investigation_id"])
    except ValueError:
        _invalid()
    _validate_transition_fields(fields)
    try:
        dossier_approval.validate_principal(fields["principal_ref"])
    except dossier_approval.ApprovalPrincipalInvalid:
        _invalid()
    recorded_at = fields["recorded_at"]
    if (
        type(recorded_at) is not str
        or dossier_store._TIMESTAMP.fullmatch(recorded_at) is None
    ):
        _invalid()
    return _plan(fields)


def _copy_review(review: object) -> dict | None:
    if review is None:
        return None
    return {
        "verdict": review["verdict"],
        "receipt_ids": list(review["receipt_ids"]),
        "note": review["note"],
    }


def refuse_client_purpose_override(
    evaluation: Mapping[str, object] | None, *, role: object, action: object
) -> None:
    """No review role and no action moves a resource whose client purpose is prohibited.

    The role and action are accepted so the refusal is visibly independent of
    both; the evaluation is the client purpose evaluation of the resource's own
    content, None for general 42, which names no client policy.
    """
    from . import client_purpose

    if evaluation is None:
        return None
    if evaluation.get("state") == "prohibited":
        _invalid(client_purpose.REVIEW_OVERRIDE_REFUSED)
    return None


def validate_review_command(command: object) -> dict:
    """The command shape the review endpoint accepts, and nothing beyond it.

    A principal reference inside the command is not ignored quietly, it makes
    the command invalid: the principal is bound server side from the validated
    credential and a command that tries to carry one is malformed.
    """
    if type(command) is not dict or set(command) != set(COMMAND_FIELDS):
        _invalid("review_request_invalid")
    if command["contract_version"] != REVIEW_CONTRACT_VERSION:
        _invalid("review_request_invalid")
    try:
        _validate_transition_fields(command)
        _plan(command)
    except DossierRecordInvalid:
        _invalid("review_request_invalid")
    return copy.deepcopy(command)


def build_decision(
    *,
    investigation_id: str,
    dossier_version: str,
    resource: str,
    resource_id: str,
    resource_version: str,
    action: str,
    expected_state: str,
    idempotency_key: str,
    support_review: Mapping[str, object] | None,
    principal_ref: str,
    recorded_at: str,
    acting_role: str | None = None,
) -> dict:
    """One decision record, validated, with its id computed from its content.

    acting_role, when given, must be the role the transition requires.
    """
    fields = {
        "investigation_id": investigation_id,
        "dossier_version": dossier_version,
        "resource": resource,
        "resource_id": resource_id,
        "resource_version": resource_version,
        "action": action,
        "expected_state": expected_state,
        "idempotency_key": idempotency_key,
        "support_review": support_review,
        "principal_ref": principal_ref,
        "recorded_at": recorded_at,
    }
    plan = _validate_core(fields)
    record = {
        "contract_version": REVIEW_CONTRACT_VERSION,
        **fields,
        "support_review": _copy_review(support_review),
        "next_state": plan.next_state,
        "state": plan.next_state,
    }
    shape = DECISION_FIELDS
    if acting_role is not None:
        if acting_role != plan.required_role:
            _invalid()
        record["acting_role"] = acting_role
        shape = ACTED_DECISION_FIELDS
    record["decision_id"] = canonical_digest(record)
    return {field: record[field] for field in shape}


def parse_decision(record: object) -> dict:
    """A stored or offered decision, admitted only when it is exactly itself."""
    if type(record) is not dict or set(record) not in (
        set(DECISION_FIELDS),
        set(ACTED_DECISION_FIELDS),
    ):
        _invalid()
    if record["contract_version"] != REVIEW_CONTRACT_VERSION:
        _invalid()
    plan = _validate_core(record)
    if record["next_state"] != plan.next_state or record["state"] != plan.next_state:
        _invalid()
    if "acting_role" in record and record["acting_role"] != plan.required_role:
        _invalid()
    rest = {key: value for key, value in record.items() if key != "decision_id"}
    if record["decision_id"] != canonical_digest(rest):
        _invalid()
    return copy.deepcopy(record)


def decision_directory(
    prefix: object, investigation_id: object, version: object
) -> str:
    dossier_store._validate_prefix(prefix)
    dossier_store._validate_investigation_id(investigation_id)
    dossier_store._validate_version(version)
    return f"{prefix}decisions/{investigation_id}/{version}/"


def decision_object_name(
    prefix: object, investigation_id: object, version: object, decision_id: object
) -> str:
    directory = decision_directory(prefix, investigation_id, version)
    if not dossier_store._digest(decision_id):
        raise ValueError("decision id is invalid")
    return f"{directory}{decision_id}.json"


def create_decision(*, bucket: object, prefix: str, decision: Mapping) -> dict:
    """Store one decision once. An identical retry reads back what is stored."""
    record = parse_decision(decision)
    body = canonical_bytes(record)
    name = decision_object_name(
        prefix,
        record["investigation_id"],
        record["dossier_version"],
        record["decision_id"],
    )
    written = dossier_store.create_once(
        bucket=bucket,
        name=name,
        body=body,
        conflict_code="decision_conflict",
        ambiguous_code="decision_create_ambiguous",
    )
    return {
        "created": written["created"],
        "decision": record,
        "generation": written["generation"],
        "object_name": name,
    }


def read_decision(
    *,
    bucket: object,
    prefix: str,
    investigation_id: str,
    dossier_version: str,
    decision_id: str,
) -> dict:
    """One stored decision by id, admitted only as the canonical record it names."""
    name = decision_object_name(prefix, investigation_id, dossier_version, decision_id)
    found = dossier_store._read_object(bucket, name)
    if found is None:
        raise DossierStoreError("decision_unreadable")
    raw, _generation = found
    record = parse_decision(dossier_store._load(raw))
    if canonical_bytes(record) != raw or record["decision_id"] != decision_id:
        _invalid()
    return record


def _instant(recorded_at: str) -> str:
    # The timestamp grammar allows one to six fractional digits or none. Padded
    # to six, the text orders the way the instants do; unpadded, "05.5Z" would
    # sort before "05Z".
    base, _, fraction = recorded_at[:-1].partition(".")
    return base + "." + fraction.ljust(6, "0")


def _recorded_order_instant(record: Mapping[str, object]) -> str:
    return _instant(record["recorded_at"])


def _recorded_order(record: Mapping[str, object]) -> tuple[str, str]:
    return _instant(record["recorded_at"]), record["decision_id"]


def list_decisions(
    *, bucket: object, prefix: str, investigation_id: str, dossier_version: str
) -> list[dict]:
    """Every decision recorded for one dossier version, in recorded order.

    Each object is read pinned to its listed generation and admitted only when
    its bytes are the canonical form of a valid record whose id names the
    object and whose identity is the one listed. Objects under the directory
    that are not named like a decision are not decisions and are left alone.
    """
    directory = decision_directory(prefix, investigation_id, dossier_version)
    try:
        # The delimiter keeps the listing to this directory, so the state
        # pointer entries under pointer/ are never walked here.
        names = [
            blob.name for blob in bucket.list_blobs(prefix=directory, delimiter="/")
        ]
    except Exception as error:
        raise DossierStoreError("dossier_store_unavailable") from error
    decisions = []
    for name in names:
        if type(name) is not str or not name.startswith(directory):
            raise DossierStoreError("dossier_store_unavailable")
        tail = name[len(directory) :]
        if _DECISION_NAME.fullmatch(tail) is None:
            continue
        found = dossier_store._read_object(bucket, name)
        if found is None:
            # Listed and then gone. The store never deletes, so the storage
            # cannot be trusted to answer for this version right now.
            raise DossierStoreError("dossier_store_unavailable")
        raw, _generation = found
        try:
            record = parse_decision(dossier_store._load(raw))
        except DossierRecordInvalid:
            _invalid()
        if (
            canonical_bytes(record) != raw
            or tail != record["decision_id"] + ".json"
            or record["investigation_id"] != investigation_id
            or record["dossier_version"] != dossier_version
        ):
            _invalid()
        decisions.append(record)
    decisions.sort(key=_recorded_order)
    return decisions


def replay_state(
    decisions: Sequence[Mapping[str, object]], *, resource: str, resource_id: str
) -> dict:
    """The state one resource reaches when its decisions replay in order.

    A decision whose expected state is not the state reached so far was made
    against a state that had already moved. It stays in the history and is not
    applied, which is what makes the replay a function of the record set and
    not of who read it. Decisions recorded at the same instant have no order
    between them, so within one instant the decision that continues the chain
    is applied first, and by id when more than one could; a decision from an
    earlier instant is never revived by a later one. One resource inside one
    version has one content digest, so decisions that disagree about it are a
    broken store, not a state.
    """
    if resource not in RESOURCES:
        raise ValueError("resource is invalid")
    mine = sorted(
        (
            decision
            for decision in decisions
            if decision["resource"] == resource
            and decision["resource_id"] == resource_id
        ),
        key=_recorded_order,
    )
    version = None
    for decision in mine:
        if version is None:
            version = decision["resource_version"]
        elif decision["resource_version"] != version:
            _invalid("decision_resource_version_conflict")
    state = dossier_approval.initial_state(resource)
    applied = []
    for _instant_key, group in groupby(mine, key=_recorded_order_instant):
        pending = list(group)
        while True:
            match = next(
                (item for item in pending if item["expected_state"] == state), None
            )
            if match is None:
                break
            pending.remove(match)
            state = match["next_state"]
            applied.append(match["decision_id"])
    return {
        "resource": resource,
        "resource_id": resource_id,
        "state": state,
        "resource_version": version,
        "applied": applied,
    }


def resource_state(
    *,
    bucket: object,
    prefix: str,
    investigation_id: str,
    dossier_version: str,
    resource: str,
    resource_id: str,
) -> dict:
    """The replayed view of one resource's stored decisions; the committed state is the pointer's."""
    decisions = list_decisions(
        bucket=bucket,
        prefix=prefix,
        investigation_id=investigation_id,
        dossier_version=dossier_version,
    )
    return replay_state(decisions, resource=resource, resource_id=resource_id)
# The state pointer
#
# One log per dossier version, beside the decisions, whose top entry holds the
# state each reviewed resource has reached and the ids of the decisions that
# took it there. It is kept the way dossier_store keeps the dossier pointer:
# entry n is created once under <decisions>/pointer/, names entry n - 1 by
# number and digest, and is never replaced, because the staging identity can
# create objects but not overwrite them. The generation a read reports is the
# number of the entry it read, 0 when the log is empty. A decision is committed
# by creating the entry after the one that was read, so two writers that both
# recorded a decision against the same state cannot both commit: the second
# create finds the number taken and that writer is refused. The decision
# objects stay create-only history either way.

STATE_POINTER_VERSION = "dossier_review_state_v1"
STATE_POINTER_ENTRY_VERSION = "dossier_review_state_entry_v1"
STATE_POINTER_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "resources",
)
_RESOURCE_ENTRY_FIELDS = ("state", "resource_version", "applied")


def state_pointer_object_name(
    prefix: object, investigation_id: object, version: object
) -> str:
    """The retired single object state pointer. Never written; a reader refuses it."""
    return decision_directory(prefix, investigation_id, version) + "pointer.json"


def state_pointer_directory(
    prefix: object, investigation_id: object, version: object
) -> str:
    return decision_directory(prefix, investigation_id, version) + "pointer/"


def state_pointer_entry_name(
    prefix: object, investigation_id: object, version: object, sequence: object
) -> str:
    return state_pointer_directory(
        prefix, investigation_id, version
    ) + dossier_store._entry_tail(sequence)


def _state_log(prefix: str, investigation_id: str, dossier_version: str) -> dict:
    def parse(payload: object) -> dict:
        return parse_state_pointer(
            payload, investigation_id=investigation_id, dossier_version=dossier_version
        )

    return {
        "directory": state_pointer_directory(prefix, investigation_id, dossier_version),
        "contract": STATE_POINTER_ENTRY_VERSION,
        "invalid_code": "state_pointer_invalid",
        "exhausted_code": "state_pointer_history_exhausted",
        "parse_payload": parse,
    }


def _empty_state_pointer(investigation_id: str, dossier_version: str) -> dict:
    return {
        "contract_version": STATE_POINTER_VERSION,
        "investigation_id": investigation_id,
        "dossier_version": dossier_version,
        "resources": {},
    }


def _resource_key(resource: str, resource_id: str) -> str:
    return f"{resource}:{resource_id}"


def parse_state_pointer(
    record: object, *, investigation_id: str, dossier_version: str
) -> dict:
    """A stored pointer, admitted only when it is exactly itself and about this version."""
    if type(record) is not dict or set(record) != set(STATE_POINTER_FIELDS):
        _invalid("state_pointer_invalid")
    if (
        record["contract_version"] != STATE_POINTER_VERSION
        or record["investigation_id"] != investigation_id
        or record["dossier_version"] != dossier_version
        or type(record["resources"]) is not dict
    ):
        _invalid("state_pointer_invalid")
    for key, entry in record["resources"].items():
        resource, separator, resource_id = key.partition(":")
        if not separator or resource not in RESOURCES or not _bounded_text(resource_id):
            _invalid("state_pointer_invalid")
        if type(entry) is not dict or set(entry) != set(_RESOURCE_ENTRY_FIELDS):
            _invalid("state_pointer_invalid")
        applied = entry["applied"]
        if (
            not _bounded_text(entry["state"])
            or not dossier_store._digest(entry["resource_version"])
            or type(applied) is not list
            or not applied
            or not all(dossier_store._digest(item) for item in applied)
            or len(set(applied)) != len(applied)
        ):
            _invalid("state_pointer_invalid")
    return copy.deepcopy(record)


def read_state_pointer(
    *, bucket: object, prefix: str, investigation_id: str, dossier_version: str
) -> dict:
    """The version's pointer with the entry number it was read at; 0 when absent."""
    log = _state_log(prefix, investigation_id, dossier_version)
    head = dossier_store.read_pointer_log(
        bucket,
        directory=log["directory"],
        retired_name=state_pointer_object_name(prefix, investigation_id, dossier_version),
        contract=log["contract"],
        invalid_code=log["invalid_code"],
        exhausted_code=log["exhausted_code"],
        parse_payload=log["parse_payload"],
    )
    if head is None:
        return {
            "pointer": _empty_state_pointer(investigation_id, dossier_version),
            "generation": 0,
        }
    return {"pointer": head["payload"], "generation": head["sequence"]}


def _held_entry(
    bucket: object, prefix: str, investigation_id: str, dossier_version: str, current
) -> dict | None:
    """The entry a caller's read names, as the head the next entry must follow."""
    sequence = current["generation"]
    if type(sequence) is not int or sequence < 0:
        _invalid("state_pointer_invalid")
    if sequence == 0:
        return None
    log = _state_log(prefix, investigation_id, dossier_version)
    entry, raw, _generation = dossier_store._read_entry(
        bucket,
        log["directory"],
        sequence,
        contract=log["contract"],
        invalid_code=log["invalid_code"],
        parse_payload=log["parse_payload"],
    )
    if entry["pointer"] != current["pointer"]:
        # The entry is immutable, so a read that disagrees with it was not
        # a read of this log.
        _invalid("state_pointer_invalid")
    return {"sequence": sequence, "sha256": hashlib.sha256(raw).hexdigest()}


def resource_pointer_state(
    pointer: Mapping[str, object], *, resource: str, resource_id: str
) -> dict:
    """The state the pointer holds for one resource, or the grammar's initial state."""
    entry = pointer["resources"].get(_resource_key(resource, resource_id))
    if entry is None:
        return {
            "state": dossier_approval.initial_state(resource),
            "resource_version": None,
            "applied": [],
        }
    return copy.deepcopy(entry)


def advance_state_pointer(
    *,
    bucket: object,
    prefix: str,
    decision: Mapping,
    current: Mapping[str, object] | None = None,
) -> dict:
    """Commit one recorded decision by creating the entry after the one read.

    current is a read_state_pointer result the caller already holds; without it
    the pointer is read here. A decision the pointer already applied moves
    nothing and reports applied False. A decision whose expected state is not
    the state the pointer holds is state_pointer_conflict and nothing is
    written. A precondition that fails because the pointer moved under the
    caller is reread: when the resource is still where the decision expects it
    the refusal is state_pointer_generation_moved, which the same command can
    retry, and otherwise it is state_pointer_conflict.
    """
    record = parse_decision(decision)
    investigation_id = record["investigation_id"]
    version = record["dossier_version"]
    if current is None:
        current = read_state_pointer(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=version,
        )
    entry = resource_pointer_state(
        current["pointer"], resource=record["resource"], resource_id=record["resource_id"]
    )
    if record["decision_id"] in entry["applied"]:
        return {
            "pointer": copy.deepcopy(current["pointer"]),
            "generation": current["generation"],
            "applied": False,
        }
    if not _entry_admits(entry, record):
        raise DossierConflict("state_pointer_conflict")
    pointer = copy.deepcopy(current["pointer"])
    pointer["resources"][_resource_key(record["resource"], record["resource_id"])] = {
        "state": record["next_state"],
        "resource_version": record["resource_version"],
        "applied": [*entry["applied"], record["decision_id"]],
    }
    head = _held_entry(bucket, prefix, investigation_id, version, current)
    log = _state_log(prefix, investigation_id, version)
    try:
        dossier_store.append_pointer_log(
            bucket,
            directory=log["directory"],
            contract=log["contract"],
            exhausted_code=log["exhausted_code"],
            head=head,
            payload=pointer,
        )
    except PreconditionFailed:
        moved = read_state_pointer(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=version,
        )
        entry = resource_pointer_state(
            moved["pointer"], resource=record["resource"], resource_id=record["resource_id"]
        )
        if record["decision_id"] in entry["applied"]:
            # An identical writer committed this very decision first.
            return {
                "pointer": moved["pointer"],
                "generation": moved["generation"],
                "applied": False,
            }
        if not _entry_admits(entry, record):
            raise DossierConflict("state_pointer_conflict") from None
        raise DossierConflict("state_pointer_generation_moved") from None
    return {"pointer": pointer, "generation": current["generation"] + 1, "applied": True}


def _entry_admits(entry: Mapping[str, object], record: Mapping[str, object]) -> bool:
    return entry["state"] == record["expected_state"] and (
        entry["resource_version"] is None
        or entry["resource_version"] == record["resource_version"]
    )


def applied_decisions(
    decisions: Sequence[Mapping[str, object]], pointer: Mapping[str, object]
) -> list[dict]:
    """The decisions the pointer committed, in the order given, and no other."""
    applied = {
        item for entry in pointer["resources"].values() for item in entry["applied"]
    }
    return [copy.deepcopy(item) for item in decisions if item["decision_id"] in applied]
