"""The consolidated Intelligence Console and Evidence Room dossier read.

One request returns one investigation's working paper: its question, claims,
the evidence under them, what contradicts them, and how ready the artifact is.

The projection is allowlist based and it happens here, on the server. Material
that fails a gate never enters the response, so no later client mistake can
leak it. What it does carry is the fact that something was excluded and why,
because a dossier that quietly drops its own disagreement reads as a consensus
that was never reached.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from collections import Counter
import re
from typing import Any

CONTRACT_VERSION = "intelligence_dossier_v1"

APPROVED = "approved"
READY = "ready"

# Kinds a client may see stated as a finding.
FINDING_KINDS = frozenset({"observation", "interpretation", "recommendation"})
# The one place thin, contradictory or unchecked evidence may be spoken aloud,
# because saying so is the entire point of the section.
LIMITATION_KINDS = frozenset({"limitation", "abstention"})
# Internal reasoning and internal machinery. Never projected, at any status.
NEVER_PROJECTED_KINDS = frozenset(
    {"model_proposal", "model_memory", "internal_assumption", "diffusion_shadow"}
)

ARTIFACT_STATES = ("client_ready", "approval_required", "blocked")


class DossierContractError(ValueError):
    """The dossier response violates its contract."""


def _claim_exclusion_reason(claim: dict[str, Any]) -> str | None:
    kind = claim.get("kind")
    if not isinstance(kind, str):
        return "claim kind is unrecognized"
    if kind in NEVER_PROJECTED_KINDS:
        return "kind is never projected to a client"
    if claim.get("status") != APPROVED:
        status = claim.get("status")
        label = (
            status
            if isinstance(status, str)
            and status in {"pending", "rejected", "superseded", "disputed"}
            else "unrecognized"
        )
        return f"claim status is {label}, not approved"
    if claim.get("selected") is not True:
        return "claim is not selected for the artifact"
    if not isinstance(claim.get("text"), str) or not claim["text"].strip():
        return "claim has no usable text"
    citations = claim.get("citations")
    if (
        not isinstance(citations, list)
        or not citations
        or any(not isinstance(item, str) or not item.strip() for item in citations)
    ):
        return "claim carries no citation"
    if kind in LIMITATION_KINDS:
        return None
    if kind not in FINDING_KINDS:
        return "kind is not a client claim kind"
    if claim.get("evidence_state") != READY:
        state = claim.get("evidence_state")
        label = (
            state
            if isinstance(state, str)
            and state in {"thin", "contradictory", "unchecked", "withheld"}
            else "unrecognized"
        )
        return f"evidence state is {label}, which is admissible only as a limitation"
    return None


def _evidence_exclusion_reason(item: dict[str, Any], cutoff: object) -> str | None:
    """Why one evidence item may not travel to a client, or None.

    These are the same three gates the client projection applies. They are
    stated here as well, and here is the one that counts: this projection
    exists so that material failing a gate never reaches the wire, and a rule
    enforced only in the browser is a rule a browser mistake can undo.
    """
    if item.get("client_citable") is not True:
        return "evidence is not client citable"
    if item.get("in_scope") is not True:
        return "evidence is outside the investigation scope"
    published_at = item.get("published_at")
    if not isinstance(published_at, str) or not published_at:
        # Undated evidence cannot be checked against the cutoff. Unknown is
        # not early, and admitting it would turn a missing value into a
        # passing gate, the one direction this projection may never fail in.
        return "evidence carries no publication date, so it cannot be checked against the cutoff"
    limit = _cutoff_limit(cutoff)
    if limit is None:
        # Absent and unreadable are the same fact here: there is no boundary
        # this date can be checked against.
        return (
            "investigation cutoff is unavailable, so evidence dating cannot be checked"
        )
    at = _parse_datetime(published_at)
    if at is None:
        return (
            "evidence dating is unreadable, so it cannot be checked against the cutoff"
        )
    if at >= limit:
        return "evidence is dated after the investigation cutoff"
    return None


def _cutoff_limit(cutoff: object):
    """The end of the cutoff day, or None when no usable boundary exists.

    The boundary is the end of that day, not its start, because same day
    evidence is usually the most recent proof a claim rests on. A cutoff that
    is absent, blank or unreadable yields None: there is no boundary, and the
    caller must refuse rather than treat no boundary as a wide one.
    """
    if not isinstance(cutoff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
        return None
    try:
        return datetime.fromisoformat(f"{cutoff}T00:00:00+00:00") + timedelta(days=1)
    except (ValueError, OverflowError):
        return None


def _parse_datetime(value: str):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?",
        value,
    ):
        return None
    try:
        offset = re.search(r"[+-](\d{2}):(\d{2})$", value)
        if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
            return None
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def read_dossier(investigation_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Project one stored investigation into its dossier read."""
    # The id is not a caption. It names the scope this content was approved
    # within, so a record that belongs to another investigation, or that never
    # said which one it belongs to, cannot be published under it. Echoing the
    # caller's id over a foreign record would produce a document that is wrong
    # in the one way nobody would think to check.
    if record.get("investigation_id") != investigation_id:
        raise DossierContractError(
            "dossier record does not belong to this investigation"
        )
    claims: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    source_claims = record.get("claims", [])
    source_evidence = record.get("evidence", [])
    if (
        not isinstance(source_claims, list)
        or not isinstance(source_evidence, list)
        or any(not isinstance(item, dict) for item in source_claims + source_evidence)
    ):
        raise DossierContractError("dossier resources must be records")
    claim_ids = Counter(
        item.get("claim_id")
        for item in source_claims
        if isinstance(item.get("claim_id"), str) and item["claim_id"].strip()
    )
    evidence_ids = Counter(
        item.get("evidence_id")
        for item in source_evidence
        if isinstance(item.get("evidence_id"), str) and item["evidence_id"].strip()
    )
    eligible = {
        item["evidence_id"]
        for item in source_evidence
        if isinstance(item.get("evidence_id"), str)
        and evidence_ids[item["evidence_id"]] == 1
        and _evidence_exclusion_reason(item, record.get("cutoff")) is None
    }

    for claim in source_claims:
        reason = _claim_exclusion_reason(claim)
        if not reason and (
            not isinstance(claim.get("claim_id"), str)
            or claim_ids[claim["claim_id"]] != 1
        ):
            reason = "claim identity is missing or ambiguous"
        if reason:
            excluded.append(
                {
                    "resource_id": claim.get("claim_id"),
                    "resource_kind": "claim",
                    "excluded_from": "client_read",
                    "reason": reason,
                }
            )
            continue
        claims.append(
            {
                "claim_id": claim["claim_id"],
                "kind": claim["kind"],
                "text": claim["text"],
                "citations": list(claim.get("citations", [])),
            }
        )

    cited = {citation for claim in claims for citation in claim["citations"]}
    cutoff = record.get("cutoff")
    evidence = []
    for item in source_evidence:
        if (
            not isinstance(item.get("evidence_id"), str)
            or item["evidence_id"] not in cited
        ):
            continue
        evidence_reason = _evidence_exclusion_reason(item, cutoff)
        if not evidence_reason and evidence_ids[item["evidence_id"]] != 1:
            evidence_reason = "evidence identity is missing or ambiguous"
        if evidence_reason:
            excluded.append(
                {
                    "resource_id": item.get("evidence_id"),
                    "resource_kind": "evidence",
                    "excluded_from": "client_read",
                    "reason": evidence_reason,
                }
            )
            continue
        evidence.append(
            {
                "evidence_id": item["evidence_id"],
                "published_at": item.get("published_at"),
            }
        )

    supported_claims = []
    for claim in claims:
        if _citations_resolve(claim, eligible):
            supported_claims.append(claim)
        else:
            excluded.append(
                {
                    "resource_id": claim["claim_id"],
                    "resource_kind": "claim",
                    "excluded_from": "client_read",
                    "reason": "claim citations do not resolve to eligible evidence",
                }
            )
    claims = supported_claims
    retained = {citation for claim in claims for citation in claim["citations"]}
    evidence = [item for item in evidence if item["evidence_id"] in retained]

    failed: list[str] = []
    if (
        not isinstance(record.get("concise_answer"), str)
        or not record["concise_answer"].strip()
    ):
        failed.append("concise_answer")
    if record.get("decision") != APPROVED:
        failed.append("decision")
    selected = [
        item
        for item in source_claims
        if item.get("selected") is True
        and (
            not isinstance(item.get("kind"), str)
            or item["kind"] not in NEVER_PROJECTED_KINDS
        )
    ]
    if not selected:
        failed.append("claim_not_cited")
    for item in selected:
        if item.get("status") != APPROVED and "claim_not_approved" not in failed:
            failed.append("claim_not_approved")
        if not _citations_resolve(item, eligible) and "claim_not_cited" not in failed:
            failed.append("claim_not_cited")
        if (
            not isinstance(item.get("kind"), str)
            or item["kind"] not in FINDING_KINDS | LIMITATION_KINDS
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
            or (
                item.get("kind") in FINDING_KINDS
                and item.get("evidence_state") != READY
            )
            or not isinstance(item.get("claim_id"), str)
            or claim_ids[item["claim_id"]] != 1
        ):
            if "claim_not_ready" not in failed:
                failed.append("claim_not_ready")
    for relationship in record.get("relationships", []):
        if (
            relationship.get("status") != APPROVED
            and "relationship_not_approved" not in failed
        ):
            failed.append("relationship_not_approved")
    for precedent in record.get("precedents", []):
        if (
            _cutoff_limit(precedent.get("occurred_on")) is None
            or _cutoff_limit(cutoff) is None
            or precedent["occurred_on"] >= cutoff
        ):
            if "precedent_not_earlier_than_cutoff" not in failed:
                failed.append("precedent_not_earlier_than_cutoff")
        if not all(
            isinstance(precedent.get(key), list)
            and precedent[key]
            and all(isinstance(item, str) and item.strip() for item in precedent[key])
            for key in ("material_differences", "nontransferable")
        ):
            if "precedent_missing_transfer_limits" not in failed:
                failed.append("precedent_missing_transfer_limits")
    if any(
        # Fail closed: a question that does not say blocking=False is not
        # known safe, and unknown must not read as open.
        question.get("blocking") is not False
        for question in record.get("unanswered_questions", [])
    ):
        failed.append("blocking_question")

    if not failed:
        state = "client_ready"
    elif set(failed) <= {"decision", "claim_not_approved", "relationship_not_approved"}:
        state = "approval_required"
    else:
        state = "blocked"

    payload = {
        "contract_version": CONTRACT_VERSION,
        "investigation_id": investigation_id,
        "concise_answer": record.get("concise_answer")
        if isinstance(record.get("concise_answer"), str)
        else None,
        "claims": claims,
        "evidence": evidence,
        "excluded_count": len(excluded),
        "excluded_reasons": sorted({item["reason"] for item in excluded}),
        "artifact_readiness": {"state": state, "failed": failed},
    }
    validate_dossier_payload(payload)
    return payload


def _citations_resolve(claim, eligible):
    citations = claim.get("citations")
    return (
        isinstance(citations, list)
        and bool(citations)
        and all(isinstance(item, str) and item in eligible for item in citations)
        and len(set(citations)) == len(citations)
    )


def validate_dossier_payload(payload: dict[str, Any]) -> None:
    """The response contract, asserted on the way out."""
    expected = (
        "contract_version",
        "investigation_id",
        "concise_answer",
        "claims",
        "evidence",
        "excluded_count",
        "excluded_reasons",
        "artifact_readiness",
    )
    if tuple(payload) != expected:
        raise DossierContractError("dossier response field order is invalid")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise DossierContractError("dossier contract version is unsupported")
    if payload["concise_answer"] is not None and not isinstance(
        payload["concise_answer"], str
    ):
        raise DossierContractError("projected answer must be text or null")
    if payload["artifact_readiness"]["state"] not in ARTIFACT_STATES:
        raise DossierContractError("dossier artifact state is unsupported")
    # The excluded material is counted and its reasons named. Its text is not
    # carried, so a client cannot receive what the gates withheld.
    if payload["excluded_count"] and not payload["excluded_reasons"]:
        raise DossierContractError("excluded material must carry its reasons")
    evidence_ids = [item.get("evidence_id") for item in payload["evidence"]]
    claim_ids = [item.get("claim_id") for item in payload["claims"]]
    for identities in (evidence_ids, claim_ids):
        if any(
            not isinstance(item, str) or not item.strip() for item in identities
        ) or len(set(identities)) != len(identities):
            raise DossierContractError("projected resource identities must be unique")
    for claim in payload["claims"]:
        if (
            set(claim) != {"claim_id", "kind", "text", "citations"}
            or claim["kind"] not in FINDING_KINDS | LIMITATION_KINDS
            or not isinstance(claim["text"], str)
            or not claim["text"].strip()
        ):
            raise DossierContractError("projected claim is outside the allowlist")
        if not _citations_resolve(claim, set(evidence_ids)):
            raise DossierContractError("projected claim citations must resolve")
    for item in payload["evidence"]:
        if (
            set(item) != {"evidence_id", "published_at"}
            or _parse_datetime(item["published_at"]) is None
        ):
            raise DossierContractError("projected evidence is outside the allowlist")
    if payload["artifact_readiness"]["state"] == "client_ready" and (
        not payload["claims"]
        or not payload["concise_answer"]
        or payload["artifact_readiness"]["failed"]
    ):
        raise DossierContractError("a ready dossier must contain supported claims")
