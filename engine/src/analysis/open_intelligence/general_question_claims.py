"""Structural claim checks; source admission and textual support remain separate."""

from __future__ import annotations

import copy
from collections import deque

_CLAIM_FIELDS = {
    "claim_id",
    "text",
    "kind",
    "receipt_ids",
    "reading_ids",
    "parent_claim_ids",
    "support_state",
    "limitations",
    "falsifier",
}
_SECTION_ORDER = ("answer", "evidence", "interpretation", "actions")


def _invalid():
    raise ValueError("claim_structure_invalid")


def _text(value):
    if type(value) is not str or not value.strip():
        _invalid()
    value.encode("utf-8")
    return value


def _strings(value, *, unique=False):
    if type(value) is not list:
        _invalid()
    for item in value:
        _text(item)
    if unique and len(set(value)) != len(value):
        _invalid()
    return value


def validate_question_claim_structure(value, *, receipt_ids, reading_receipts):
    if type(value) is not dict or set(value) != {"claims", "sections"}:
        _invalid()
    receipts = set(_strings(receipt_ids, unique=True))
    if type(reading_receipts) is not dict:
        _invalid()
    for reading_id, sources in reading_receipts.items():
        _text(reading_id)
        if not _strings(sources, unique=True) or not set(sources) <= receipts:
            _invalid()
    claims = value["claims"]
    sections = value["sections"]
    if type(claims) is not list or type(sections) is not list:
        _invalid()
    indexed = {}
    review_required = False
    for claim in claims:
        if type(claim) is not dict or set(claim) != _CLAIM_FIELDS:
            _invalid()
        cid = _text(claim["claim_id"])
        if cid in indexed:
            _invalid()
        _text(claim["text"])
        if claim["kind"] not in ("observation", "interpretation", "inference", "proposal") or claim[
            "support_state"
        ] not in ("source_record", "derived", "proposed"):
            _invalid()
        if (claim["kind"] == "proposal") != (claim["support_state"] == "proposed"):
            _invalid()
        _strings(claim["limitations"])
        if claim["falsifier"] is not None:
            _text(claim["falsifier"])
        if claim["kind"] == "proposal":
            _text(claim["falsifier"])
            review_required = True
        for field, allowed in (("receipt_ids", receipts), ("reading_ids", reading_receipts)):
            if not set(_strings(claim[field], unique=True)) <= set(allowed):
                _invalid()
        _strings(claim["parent_claim_ids"], unique=True)
        indexed[cid] = claim
    children = {cid: [] for cid in indexed}
    remaining = {}
    for cid, claim in indexed.items():
        parents = claim["parent_claim_ids"]
        if not set(parents) <= indexed.keys():
            _invalid()
        remaining[cid] = len(parents)
        for parent in parents:
            children[parent].append(cid)
    pending = deque(cid for cid, count in remaining.items() if count == 0)
    basis = {}
    while pending:
        cid = pending.popleft()
        claim = indexed[cid]
        has_basis = bool(claim["receipt_ids"] or claim["reading_ids"]) or any(
            basis[parent] for parent in claim["parent_claim_ids"]
        )
        if not has_basis:
            _invalid()
        basis[cid] = claim["kind"] != "proposal" and has_basis
        for child in children[cid]:
            remaining[child] -= 1
            if remaining[child] == 0:
                pending.append(child)
    if len(basis) != len(indexed):
        _invalid()
    placed = set()
    prior = -1
    answer_has_reference_basis = False
    for section in sections:
        if type(section) is not dict or set(section) != {"kind", "claim_ids"}:
            _invalid()
        if section["kind"] not in _SECTION_ORDER:
            _invalid()
        order = _SECTION_ORDER.index(section["kind"])
        ids = _strings(section["claim_ids"], unique=True)
        if order <= prior or not ids or not set(ids) <= indexed.keys() or placed.intersection(ids):
            _invalid()
        prior = order
        placed.update(ids)
        for cid in ids:
            if indexed[cid]["kind"] == "proposal" and section["kind"] != "actions":
                _invalid()
            if section["kind"] == "answer" and basis[cid]:
                answer_has_reference_basis = True
    if placed != set(indexed):
        _invalid()
    return {
        "claims": copy.deepcopy(claims),
        "sections": copy.deepcopy(sections),
        "answer_has_reference_basis": answer_has_reference_basis,
        "review_required": review_required,
        "ready_for_downstream": False,
        "support_validation_required": True,
    }
