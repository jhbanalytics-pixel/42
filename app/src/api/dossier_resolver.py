"""Resolve one investigation's dossier and project it for the existing reader.

The resolver replaces the read of a field the scope never had. It follows the
scoped current pointer to an exact immutable version, or reads the version it
is asked for, and hands back either a ready resolution carrying the complete
record or a named unavailable state. It never manufactures an empty dossier
that would read as ready.

The projection turns the stored record and the authenticated review decisions
into the eight fields the existing Client Read projection consumes. Readiness
is computed here from admitted sources and exact-version human verdicts, so a
label the model wrote about its own support can never promote a finding.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from . import dossier_approval, dossier_store, intelligence_dossier, workspace_scope

RESOLUTION_VERSION = "dossier_resolution_v1"
WORKING_VERSION = "dossier_working_v1"
REVIEW_CONTRACT_VERSION = "dossier_review_v1"

# Kinds the reader knows. Everything else, including the engine's inference
# and proposal kinds and any internal kind, is excluded from the projection.
PROJECTED_KINDS = frozenset(
    {"observation", "interpretation", "recommendation", "limitation", "abstention"}
)
DECISION_RESOURCES = frozenset({"claim", "relationship", "artifact"})
DECISION_STATES = frozenset({"approved", "rejected"})
SUPPORT_VERDICTS = frozenset({"supported", "partial", "contradictory", "unsupported"})
# The reader states a human verdict maps to. Unknown review stays unchecked.
_VERDICT_STATES = {
    "supported": "ready",
    "partial": "thin",
    "contradictory": "contradictory",
    "unsupported": "withheld",
}


def _unavailable(
    investigation_id: str, dossier_version: str | None, reason: str
) -> dict:
    return {
        "contract_version": RESOLUTION_VERSION,
        "state": "unavailable",
        "investigation_id": investigation_id,
        "dossier_version": dossier_version,
        "generation": None,
        "dossier": None,
        "publication": None,
        "reason": reason,
    }


def resolve_dossier(
    scope: workspace_scope.ResolvedWorkspaceScope,
    dossier_version: str | None = None,
    *,
    bucket: object | None = None,
    prefix: str = dossier_store.STAGING_PREFIX,
) -> dict:
    """The scoped current version, or the exact version named, or a named absence."""
    if not isinstance(scope, workspace_scope.ResolvedWorkspaceScope):
        raise TypeError("a resolved workspace scope is required")
    if dossier_version is not None and type(dossier_version) is not str:
        raise TypeError("dossier version must be text or None")
    investigation_id = scope.investigation_id
    if bucket is None:
        try:
            bucket = workspace_scope._workspace_bucket()
        except Exception:
            # The bucket factory is storage; a factory that cannot produce a
            # bucket is storage that cannot be reached.
            return _unavailable(
                investigation_id, dossier_version, "dossier_storage_unavailable"
            )
    try:
        if dossier_version is None:
            current = dossier_store.read_pointer(
                bucket=bucket, prefix=prefix, investigation_id=investigation_id
            )
            if current is None:
                return _unavailable(investigation_id, None, "dossier_unpublished")
            pointer = current["pointer"]
            if pointer["scope_digest"] != scope.scope_digest:
                return _unavailable(investigation_id, None, "dossier_scope_mismatch")
            dossier_version = pointer["dossier_version"]
        found = dossier_store.read_dossier_version(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=dossier_version,
            scope_digest=scope.scope_digest,
        )
    except (dossier_store.DossierNotFound, dossier_store.DossierRecordInvalid) as error:
        # Absence and a refused authority each keep their own name. Neither
        # is storage failing, and neither may read as ready.
        return _unavailable(investigation_id, dossier_version, error.code)
    except ValueError:
        # A version or identity outside the store grammar is a bad request,
        # not a missing object and not an outage.
        return _unavailable(
            investigation_id, dossier_version, "dossier_request_invalid"
        )
    except dossier_store.DossierStoreError:
        # The store wraps every bucket failure into this family. Unavailable
        # is a different fact from absent, and the caller must be able to
        # tell them apart. Anything else propagates as the defect it is.
        return _unavailable(
            investigation_id, dossier_version, "dossier_storage_unavailable"
        )
    return {
        "contract_version": RESOLUTION_VERSION,
        "state": "ready",
        "investigation_id": investigation_id,
        "dossier_version": found["dossier_version"],
        "generation": found["generation"],
        "dossier": copy.deepcopy(found["record"]),
        "publication": copy.deepcopy(found["publication"]),
        "reason": None,
    }


def relationship_id(parent_claim_id: str, claim_id: str) -> str:
    digest = dossier_store.canonical_digest(
        {"claim_id": claim_id, "parent_claim_id": parent_claim_id}
    )
    return "rel_" + digest


def _authenticated(event: object, investigation_id: str, version: str) -> dict | None:
    """One decision as the review store would have written it, or None.

    A decision counts only for the exact dossier version it was made on, for
    a resource version that still matches the content, from a pseudonymous
    human reference. Anything else is not a decision about this record.
    """
    if (
        not isinstance(event, Mapping)
        or event.get("contract_version") != REVIEW_CONTRACT_VERSION
    ):
        return None
    if (
        event.get("investigation_id") != investigation_id
        or event.get("dossier_version") != version
    ):
        return None
    if (
        event.get("resource") not in DECISION_RESOURCES
        or event.get("state") not in DECISION_STATES
    ):
        return None
    if not isinstance(event.get("resource_id"), str) or not isinstance(
        event.get("resource_version"), str
    ):
        return None
    try:
        dossier_approval.validate_principal(event.get("principal_ref"))
    except dossier_approval.ApprovalPrincipalInvalid:
        return None
    review = event.get("support_review")
    if event["resource"] == "claim":
        if (
            not isinstance(review, Mapping)
            or review.get("verdict") not in SUPPORT_VERDICTS
        ):
            return None
    elif review is not None:
        return None
    return dict(event)


# The marker the engine's answer builder appends to an observation whose source
# record was retrieved by a challenge requirement and bears against the
# proposition (general_question_answer._CHALLENGE_FINDING). It is the only
# place a dossier record says that evidence contradicts something.
CHALLENGE_FINDING = "Challenge finding: this source record bears against the proposition."


def _contradicting_evidence(source_claims: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """For each claim, the receipts of the challenge findings it is or derives from.

    A claim marked as a challenge finding contradicts through its own receipts;
    a claim built on one, through its parents at any depth, carries that
    finding's receipts. Nothing else is contradicting evidence: a record that
    carries no marker yields an empty list, never an inferred one.
    """
    by_id = {
        item["claim_id"]: item
        for item in source_claims
        if isinstance(item.get("claim_id"), str)
    }

    def receipts(claim_id: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        pending = [claim_id]
        while pending:
            current = pending.pop(0)
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            item = by_id[current]
            limitations = item.get("limitations")
            if isinstance(limitations, list) and CHALLENGE_FINDING in limitations:
                for receipt in item.get("receipt_ids") or ():
                    if isinstance(receipt, str) and receipt not in found:
                        found.append(receipt)
            pending.extend(
                parent
                for parent in item.get("parent_claim_ids") or ()
                if isinstance(parent, str)
            )
        return found

    return {claim_id: receipts(claim_id) for claim_id in by_id}


def _status(states: Sequence[str]) -> str:
    if "rejected" in states:
        return "rejected"
    if "approved" in states:
        return "approved"
    return "pending"


def _evidence_items(dossier: Mapping[str, Any]) -> list[dict]:
    snapshot = dossier.get("evidence_snapshot") or {}
    receipts = snapshot.get("receipts") if isinstance(snapshot, Mapping) else None
    markets = set((dossier.get("scope") or {}).get("market_scope") or ())
    items = []
    for receipt in receipts or ():
        if not isinstance(receipt, Mapping) or not isinstance(
            receipt.get("receipt_id"), str
        ):
            continue
        published_at = receipt.get("published_at")
        items.append(
            {
                "evidence_id": receipt["receipt_id"],
                # Citable means a client could follow it: an admitted content
                # record with a source label and a resolvable address.
                "client_citable": (
                    receipt.get("kind") == "content"
                    and isinstance(receipt.get("url"), str)
                    and bool(receipt["url"])
                    and isinstance(receipt.get("source_label"), str)
                    and bool(receipt["source_label"])
                ),
                "in_scope": receipt.get("market") in markets,
                "published_at": published_at if isinstance(published_at, str) else None,
                # Every retained source is contextual evidence. Nothing here
                # can turn a context record into a promoted signal.
                "authority": "contextual",
            }
        )
    return items


def _source_gates_pass(
    citations: Sequence[str], evidence: Mapping[str, dict], cutoff: object
) -> bool:
    if not citations:
        return False
    for citation in citations:
        item = evidence.get(citation)
        if (
            item is None
            or item["client_citable"] is not True
            or item["in_scope"] is not True
        ):
            return False
        # The same three gates the Client Read projection applies, so a
        # finding cannot be ready here and excluded there.
        if (
            intelligence_dossier._evidence_exclusion_reason(dict(item), cutoff)
            is not None
        ):
            return False
    return True


def projection_record(
    dossier: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> dict:
    """The eight reader fields, computed from the record and exact-version decisions."""
    if not isinstance(dossier, Mapping) or not isinstance(decisions, Sequence):
        raise TypeError("projection needs a dossier record and a decision list")
    investigation_id = dossier["investigation_id"]
    version = dossier_store.dossier_version_for_record(dossier)
    admitted = [
        item
        for item in (
            _authenticated(event, investigation_id, version) for event in decisions
        )
        if item is not None
    ]
    intelligence = dossier["admitted_answer"]["response"]["intelligence"]
    source_claims = [
        item for item in intelligence.get("claims") or () if isinstance(item, Mapping)
    ]
    review_state = dossier["review_state"]
    selected = set(review_state.get("selected_claim_ids") or ())
    cutoff = dossier["temporal_binding"].get("source_cutoff")
    evidence = _evidence_items(dossier)
    by_evidence = {item["evidence_id"]: item for item in evidence}
    contradicting = _contradicting_evidence(source_claims)

    claim_events: dict[str, list[dict]] = {}
    relationship_events: dict[str, list[dict]] = {}
    artifact_states: list[str] = []
    for event in admitted:
        if event["resource"] == "claim":
            claim_events.setdefault(event["resource_id"], []).append(event)
        elif event["resource"] == "relationship":
            relationship_events.setdefault(event["resource_id"], []).append(event)
        else:
            artifact_states.append(event["state"])

    claims = []
    relationships = []
    for claim in source_claims:
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or claim.get("kind") not in PROJECTED_KINDS:
            continue
        content_version = dossier_store.canonical_digest(claim)
        exact = [
            event
            for event in claim_events.get(claim_id, ())
            if event["resource_version"] == content_version
        ]
        verdicts = {event["support_review"]["verdict"] for event in exact}
        citations = [
            item for item in claim.get("receipt_ids") or () if isinstance(item, str)
        ]
        if not exact or len(verdicts) != 1:
            evidence_state = "unchecked"
        else:
            evidence_state = _VERDICT_STATES[verdicts.pop()]
            if evidence_state == "ready" and not _source_gates_pass(
                citations, by_evidence, cutoff
            ):
                evidence_state = "withheld"
        claims.append(
            {
                "claim_id": claim_id,
                "kind": claim["kind"],
                "text": claim.get("text"),
                "status": _status([event["state"] for event in exact]),
                "selected": claim_id in selected,
                "evidence_state": evidence_state,
                "citations": citations,
                # Working view only: the Client Read allowlist never copies it.
                "contradicting_evidence_ids": contradicting.get(claim_id, []),
            }
        )
        for parent in claim.get("parent_claim_ids") or ():
            if not isinstance(parent, str):
                continue
            identity = relationship_id(parent, claim_id)
            content = dossier_store.canonical_digest(
                {"claim_id": claim_id, "parent_claim_id": parent}
            )
            exact_links = [
                event
                for event in relationship_events.get(identity, ())
                if event["resource_version"] == content
            ]
            relationships.append(
                {
                    "relationship_id": identity,
                    "parent_claim_id": parent,
                    "claim_id": claim_id,
                    "status": _status([event["state"] for event in exact_links]),
                }
            )

    answer_ids = [
        claim_id
        for section in intelligence.get("sections") or ()
        if isinstance(section, Mapping) and section.get("kind") == "answer"
        for claim_id in section.get("claim_ids") or ()
    ]
    texts = {
        claim["claim_id"]: claim["text"]
        for claim in source_claims
        if isinstance(claim.get("claim_id"), str) and isinstance(claim.get("text"), str)
    }
    answer_texts = [texts[claim_id] for claim_id in answer_ids if claim_id in texts]
    return {
        "investigation_id": investigation_id,
        "cutoff": cutoff,
        "concise_answer": " ".join(answer_texts) if answer_texts else None,
        "decision": _status(artifact_states),
        "claims": claims,
        "evidence": evidence,
        "relationships": relationships,
        "unanswered_questions": copy.deepcopy(
            list(review_state.get("unresolved_questions") or ())
        ),
    }


def working_response(
    resolution: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> dict:
    """The private working view: the complete record, its projection and its publication."""
    if resolution.get("contract_version") != RESOLUTION_VERSION:
        raise ValueError("dossier resolution is invalid")
    ready = resolution["state"] == "ready"
    return {
        "contract_version": WORKING_VERSION,
        "investigation_id": resolution["investigation_id"],
        "dossier_version": resolution["dossier_version"] if ready else None,
        "generation": resolution["generation"] if ready else None,
        "state": resolution["state"],
        "dossier": copy.deepcopy(resolution["dossier"]) if ready else None,
        "projection": projection_record(resolution["dossier"], decisions)
        if ready
        else None,
        "publication": copy.deepcopy(resolution["publication"]) if ready else None,
    }
def resource_versions(dossier: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The content digest of every reviewable claim and relationship in one record.

    The same rule the projection applies when it decides whether a decision is
    about the content a reviewer saw: a claim's digest is the canonical digest
    of the claim record, a relationship's is the canonical digest of its two
    claim ids under the identity relationship_id gives it. A review command
    whose resource_version is not in this map was made about something the
    record does not contain.
    """
    if not isinstance(dossier, Mapping):
        raise TypeError("resource versions need a dossier record")
    intelligence = dossier["admitted_answer"]["response"]["intelligence"]
    claims: dict[str, str] = {}
    relationships: dict[str, str] = {}
    for item in intelligence.get("claims") or ():
        if not isinstance(item, Mapping):
            continue
        claim_id = item.get("claim_id")
        if not isinstance(claim_id, str) or item.get("kind") not in PROJECTED_KINDS:
            continue
        claims[claim_id] = dossier_store.canonical_digest(item)
        for parent in item.get("parent_claim_ids") or ():
            if not isinstance(parent, str):
                continue
            relationships[relationship_id(parent, claim_id)] = (
                dossier_store.canonical_digest(
                    {"claim_id": claim_id, "parent_claim_id": parent}
                )
            )
    return {"claim": claims, "relationship": relationships}
