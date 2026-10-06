"""Immutable investigation dossier bodies: one exact byte sequence per version.

A dossier version is the SHA256 of the bytes this module emits, so every field
that changes the record changes the version. The frame is the app's existing
investigation frame payload and its identity rule lives in the app package,
which the engine cannot import; identity is therefore established through the
injected frame validator and never taken from the caller's word.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable
from datetime import date, datetime

from src.analysis.open_intelligence.brain_contract import (
    APPROVED_MARKETS,
    canonical_bytes,
    canonical_digest,
)

DOSSIER_CONTRACT_VERSION = "investigation_dossier_record_v1"
RESULT_CONTRACT_VERSION = "general_question_result_record_v1"
SNAPSHOT_CONTRACT_VERSION = "general_question_snapshot_v1"
ADMITTED_ANSWER_STATES = frozenset({"complete", "partial"})

SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
)
TIME_FIELDS = ("window_start", "window_end", "as_of", "source_cutoff")
REQUEST_REFERENCE_FIELDS = (
    "request_id",
    "request_digest",
    "thread_anchor_request_id",
    "parent_request_id",
)
BINDING_FIELDS = (
    "policy_digest",
    "deployment_digest",
    "source_binding_digest",
    "snapshot_digest",
    "result_digest",
    "prompt_manifest_digest",
    "publisher_build_digest",
)
REVIEW_STATE_FIELDS = (
    "selected_claim_ids",
    "claim_decision_refs",
    "relationship_decision_refs",
    "unresolved_questions",
)
QUESTION_FIELDS = ("question_id", "text", "blocking")
RECORD_FIELDS = (
    "contract_version",
    "investigation_id",
    "frame",
    "scope",
    "temporal_binding",
    "request_reference",
    "admitted_answer",
    "evidence_snapshot",
    "bindings",
    "review_state",
    "predecessor_version",
    "created_at",
)
_RESULT_FIELDS = frozenset(
    {
        "contract_version",
        "request_id",
        "request_digest",
        "intake_digest",
        "policy_digest",
        "deployment_digest",
        "plan_digest",
        "snapshot_digest",
        "state",
        "recorded_at",
        "response",
    }
)
_INVESTIGATION_ID = re.compile(r"inv_[A-Za-z0-9_-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class DossierRecordInvalid(ValueError):
    def __init__(self, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__(code if field is None else f"{code}:{field}")


def _refuse(code: str, field: str | None = None) -> None:
    raise DossierRecordInvalid(code, field)


def _json_native(value: object) -> bool:
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_native(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_native(item) for key, item in value.items())
    return False


def _object(value: object, code: str, field: str | None = None) -> dict:
    if type(value) is not dict or not value or not _json_native(value):
        _refuse(code, field)
    return value


def _identifier(value: object, code: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        _refuse(code, field)
    return value


def _optional_identifier(value: object, code: str, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, code, field)


def _digest(value: object, code: str, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _refuse(code, field)
    return value


def _identifiers(value: object, code: str, field: str) -> list[str]:
    if type(value) is not list:
        _refuse(code, field)
    for item in value:
        _identifier(item, code, field)
    if len(set(value)) != len(value):
        _refuse(code, field)
    return value


def _date(value: object, code: str, field: str) -> date:
    if type(value) is not str or _DATE.fullmatch(value) is None:
        _refuse(code, field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        _refuse(code, field)


def _timestamp(value: object, code: str, field: str) -> datetime:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        _refuse(code, field)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _refuse(code, field)


def _exact_fields(value: object, fields: tuple[str, ...], code: str) -> dict:
    if type(value) is not dict or set(value) != set(fields):
        _refuse(code)
    return value


def _validate_scope(scope: object, frame: dict) -> dict:
    value = _exact_fields(scope, SCOPE_FIELDS, "scope_invalid")
    _identifier(value["client_scope_id"], "scope_invalid", "client_scope_id")
    markets = _identifiers(value["market_scope"], "scope_invalid", "market_scope")
    if not markets or not set(markets) <= APPROVED_MARKETS:
        _refuse("scope_invalid", "market_scope")
    _optional_identifier(value["brand_config_id"], "scope_invalid", "brand_config_id")
    _identifiers(value["audience_lens_ids"], "scope_invalid", "audience_lens_ids")
    _optional_identifier(value["theme_id"], "scope_invalid", "theme_id")
    _identifier(value["run_id"], "scope_invalid", "run_id")
    _identifier(value["contract_version"], "scope_invalid", "contract_version")
    for field in SCOPE_FIELDS:
        if value[field] != frame.get(field):
            _refuse("scope_frame_mismatch", field)
    return value


def _validate_temporal_binding(binding: object, snapshot: dict) -> dict:
    code = "temporal_binding_invalid"
    value = _exact_fields(binding, TIME_FIELDS, code)
    start = _date(value["window_start"], code, "window_start")
    end = _date(value["window_end"], code, "window_end")
    if start > end:
        _refuse(code, "window_start")
    as_of = _timestamp(value["as_of"], code, "as_of")
    cutoff = _date(value["source_cutoff"], code, "source_cutoff")
    if cutoff > as_of.date():
        _refuse(code, "source_cutoff")
    if value["as_of"] != snapshot.get("as_of"):
        _refuse("temporal_binding_mismatch", "as_of")
    # The engine window is {start, end, closed}. The C02 projection carries
    # start and end; closed is validated here against the same rule the plan
    # applies and travels verbatim inside the retained snapshot and answer.
    window = snapshot["window"]
    if window["start"] != value["window_start"]:
        _refuse("temporal_binding_mismatch", "window_start")
    if window["end"] != value["window_end"]:
        _refuse("temporal_binding_mismatch", "window_end")
    if window["closed"] and end >= as_of.date():
        _refuse("temporal_binding_mismatch", "window_closed")
    return value


def _validate_request_reference(reference: object) -> dict:
    code = "request_reference_invalid"
    value = _exact_fields(reference, REQUEST_REFERENCE_FIELDS, code)
    _identifier(value["request_id"], code, "request_id")
    _digest(value["request_digest"], code, "request_digest")
    _optional_identifier(value["thread_anchor_request_id"], code, "thread_anchor_request_id")
    _optional_identifier(value["parent_request_id"], code, "parent_request_id")
    return value


def _validate_bindings(bindings: object) -> dict:
    code = "bindings_invalid"
    value = _exact_fields(bindings, BINDING_FIELDS, code)
    for field in BINDING_FIELDS:
        _digest(value[field], code, field)
    return value


def _validate_evidence_snapshot(snapshot: object, reference: dict, bindings: dict) -> dict:
    code = "evidence_snapshot_invalid"
    value = _object(snapshot, code)
    if value.get("contract_version") != SNAPSHOT_CONTRACT_VERSION:
        _refuse(code, "contract_version")
    _identifier(value.get("snapshot_id"), code, "snapshot_id")
    receipts = value.get("receipts")
    if type(receipts) is not list or not receipts:
        _refuse(code, "receipts")
    receipt_ids = []
    for receipt in receipts:
        if type(receipt) is not dict:
            _refuse(code, "receipts")
        receipt_ids.append(_identifier(receipt.get("receipt_id"), code, "receipts"))
    if len(set(receipt_ids)) != len(receipt_ids):
        _refuse(code, "receipts")
    window = value.get("window")
    if (
        type(window) is not dict
        or set(window) != {"start", "end", "closed"}
        or type(window["closed"]) is not bool
    ):
        _refuse(code, "window")
    _date(window["start"], code, "window")
    _date(window["end"], code, "window")
    if type(value.get("as_of")) is not str:
        _refuse(code, "as_of")
    unsigned = {key: item for key, item in value.items() if key != "snapshot_digest"}
    if (
        value.get("snapshot_digest") != canonical_digest(unsigned)
        or value["snapshot_digest"] != bindings["snapshot_digest"]
    ):
        _refuse("snapshot_substituted", "snapshot_digest")
    if (
        value.get("request_id") != reference["request_id"]
        or value.get("request_digest") != (reference["request_digest"])
    ):
        _refuse("request_substituted", "evidence_snapshot")
    for field in ("policy_digest", "deployment_digest"):
        if value.get(field) != bindings[field]:
            _refuse("bindings_mismatch", field)
    if _source_binding_digest(value.get("provenance")) != bindings["source_binding_digest"]:
        _refuse("source_binding_mismatch", "source_binding_digest")
    return value


def _source_binding_digest(provenance: object) -> str | None:
    """The digest that binds the snapshot to its source authority, per profile.

    A protected context snapshot carries the capture binding digest. A released
    evidence snapshot carries the result digest of the source query it was
    selected from. Either is the exact source the evidence came from; a
    snapshot carrying neither cannot be bound and is refused by the caller.
    """
    if type(provenance) is not dict:
        return None
    source_binding = provenance.get("source_binding")
    if type(source_binding) is dict:
        digest = source_binding.get("source_binding_digest")
        return digest if type(digest) is str else None
    source = provenance.get("source")
    receipt = source.get("receipt") if type(source) is dict else None
    digest = receipt.get("result_digest") if type(receipt) is dict else None
    return digest if type(digest) is str else None


def _validate_answer_scope(resolved: object, scope: dict) -> None:
    # The answer ran under a server-resolved scope of its own. It must sit
    # inside the investigation scope, or a result from another client or
    # market could be stapled onto this investigation.
    if type(resolved) is not dict or set(resolved) != set(SCOPE_FIELDS[:5]):
        _refuse("answer_scope_mismatch", "resolved_scope")
    for field in ("client_scope_id", "brand_config_id", "theme_id"):
        if resolved[field] != scope[field]:
            _refuse("answer_scope_mismatch", field)
    for field in ("market_scope", "audience_lens_ids"):
        if type(resolved[field]) is not list or not set(resolved[field]) <= set(scope[field]):
            _refuse("answer_scope_mismatch", field)
    if not resolved["market_scope"] or set(resolved["audience_lens_ids"]) != set(
        scope["audience_lens_ids"]
    ):
        _refuse("answer_scope_mismatch", "market_scope")


def _validate_admitted_answer(
    answer: object, reference: dict, snapshot: dict, bindings: dict, scope: dict
) -> tuple[dict, list[str]]:
    code = "admitted_answer_invalid"
    value = _object(answer, code)
    if set(value) != _RESULT_FIELDS:
        _refuse(code, "fields")
    if value["contract_version"] != RESULT_CONTRACT_VERSION:
        _refuse(code, "contract_version")
    if value["state"] not in ADMITTED_ANSWER_STATES:
        _refuse("answer_state_invalid", "state")
    if (
        value["request_id"] != reference["request_id"]
        or value["request_digest"] != (reference["request_digest"])
    ):
        _refuse("request_substituted", "admitted_answer")
    for field in ("policy_digest", "deployment_digest"):
        if value[field] != bindings[field]:
            _refuse("bindings_mismatch", field)
    if value["snapshot_digest"] != snapshot["snapshot_digest"]:
        _refuse("snapshot_substituted", "admitted_answer")
    if canonical_digest(value) != bindings["result_digest"]:
        _refuse("result_substituted", "result_digest")
    response = value["response"]
    intelligence = response.get("intelligence") if type(response) is dict else None
    if type(intelligence) is not dict:
        _refuse(code, "response")
    if intelligence.get("snapshot_id") != snapshot["snapshot_id"]:
        _refuse("snapshot_substituted", "snapshot_id")
    _validate_answer_scope(intelligence.get("resolved_scope"), scope)
    if (
        intelligence.get("window") != snapshot["window"]
        or intelligence.get("as_of") != (snapshot["as_of"])
    ):
        _refuse("temporal_binding_mismatch", "admitted_answer")
    claims = intelligence.get("claims")
    if type(claims) is not list:
        _refuse(code, "claims")
    receipt_ids = {receipt["receipt_id"] for receipt in snapshot["receipts"]}
    claim_ids = []
    for claim in claims:
        if type(claim) is not dict:
            _refuse(code, "claims")
        claim_ids.append(_identifier(claim.get("claim_id"), code, "claims"))
        cited = claim.get("receipt_ids")
        if type(cited) is not list or not set(cited) <= receipt_ids:
            _refuse(code, "claims")
    if len(set(claim_ids)) != len(claim_ids):
        _refuse(code, "claims")
    return value, claim_ids


def _validate_review_state(review_state: object, claim_ids: list[str]) -> dict:
    code = "review_state_invalid"
    value = _exact_fields(review_state, REVIEW_STATE_FIELDS, code)
    selected = _identifiers(value["selected_claim_ids"], code, "selected_claim_ids")
    if not set(selected) <= set(claim_ids):
        _refuse("selected_claim_unknown", "selected_claim_ids")
    _identifiers(value["claim_decision_refs"], code, "claim_decision_refs")
    _identifiers(value["relationship_decision_refs"], code, "relationship_decision_refs")
    questions = value["unresolved_questions"]
    if type(questions) is not list:
        _refuse(code, "unresolved_questions")
    question_ids = []
    for question in questions:
        if type(question) is not dict or set(question) != set(QUESTION_FIELDS):
            _refuse(code, "unresolved_questions")
        question_ids.append(_identifier(question["question_id"], code, "unresolved_questions"))
        _identifier(question["text"], code, "unresolved_questions")
        if type(question["blocking"]) is not bool:
            _refuse(code, "unresolved_questions")
    if len(set(question_ids)) != len(question_ids):
        _refuse(code, "unresolved_questions")
    return value


def build_dossier_body(
    *,
    investigation_id: str,
    frame: dict,
    scope: dict,
    temporal_binding: dict,
    request_reference: dict,
    admitted_answer: dict,
    evidence_snapshot: dict,
    bindings: dict,
    review_state: dict,
    predecessor_version: str | None,
    created_at: str,
    frame_validator: Callable[[dict], str],
) -> bytes:
    """Validate every part of the record and return its exact canonical bytes."""
    if type(investigation_id) is not str or _INVESTIGATION_ID.fullmatch(investigation_id) is None:
        _refuse("investigation_id_invalid")
    frame_value = _object(frame, "frame_invalid")
    try:
        identity = frame_validator(copy.deepcopy(frame_value))
    except ValueError as error:
        raise DossierRecordInvalid("frame_invalid") from error
    if identity != investigation_id:
        _refuse("investigation_identity_mismatch")
    scope_value = _validate_scope(scope, frame_value)
    reference = _validate_request_reference(request_reference)
    binding_values = _validate_bindings(bindings)
    snapshot = _validate_evidence_snapshot(evidence_snapshot, reference, binding_values)
    time_binding = _validate_temporal_binding(temporal_binding, snapshot)
    answer, claim_ids = _validate_admitted_answer(
        admitted_answer, reference, snapshot, binding_values, scope_value
    )
    review = _validate_review_state(review_state, claim_ids)
    if predecessor_version is not None:
        _digest(predecessor_version, "predecessor_version_invalid", "predecessor_version")
    _timestamp(created_at, "created_at_invalid", "created_at")
    record = {
        "contract_version": DOSSIER_CONTRACT_VERSION,
        "investigation_id": investigation_id,
        "frame": frame_value,
        "scope": scope_value,
        "temporal_binding": time_binding,
        "request_reference": reference,
        "admitted_answer": answer,
        "evidence_snapshot": snapshot,
        "bindings": binding_values,
        "review_state": review,
        "predecessor_version": predecessor_version,
        "created_at": created_at,
    }
    if tuple(record) != RECORD_FIELDS:
        _refuse("dossier_record_invalid")
    try:
        body = canonical_bytes(copy.deepcopy(record))
    except (TypeError, ValueError) as error:
        raise DossierRecordInvalid("dossier_record_invalid") from error
    if json.loads(body.decode("utf-8")) != record:
        _refuse("dossier_record_invalid")
    return body


def dossier_version(body: bytes) -> str:
    """The version of a dossier is the SHA256 of its exact bytes."""
    if type(body) is not bytes or not body:
        raise DossierRecordInvalid("dossier_body_invalid")
    return hashlib.sha256(body).hexdigest()
