"""Phrase and digest formulas for the recurring execution grant (C03) approval kind.

The v3 approval routine mirrors these three formulas in SQL: the approval phrase is the
fixed prefix followed by the sha256 of the proposal file the approver read, the revocation
phrase is the fixed prefix followed by the grant id and is bound inside the grant as
``revocation_phrase_sha256``, and the grant digest is the canonical digest of the grant
terms without ``revocation_state``. The full grant contract (``validate_recurring_grant``)
lives in ``recurring_grant`` on the integration branch and is not present here; this
module carries only the field set and the digest so the approval store, the operator
CLI and the durable reader agree on the key before that module lands. The integration
reconciles the two modules; the formulas here are the ones the routine text pins.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256

from src.analysis.open_intelligence.brain_contract import canonical_bytes

GRANT_CONTRACT = "42_recurring_execution_grant_v1"
APPROVAL_PHRASE_PREFIX = "Approve recurring execution grant "
REVOCATION_PHRASE_PREFIX = "Revoke recurring execution grant "
GRANT_FIELDS = (
    "allowed_operations",
    "approved_release_manifest_digest",
    "contract_version",
    "cumulative_ceiling_micro_usd",
    "environment",
    "executing_principals",
    "grant_id",
    "issuing_principal",
    "monthly_allowance_micro_usd",
    "permitted_cutoffs",
    "permitted_image_digests",
    "reserved_micro_usd_per_capture",
    "resource_manifest_digest",
    "retry_rules",
    "revocation_phrase_sha256",
    "revocation_state",
    "run_allowance_micro_usd",
    "schema_version",
    "source_policy_digest",
    "valid_from",
    "valid_until",
)
REVOCATION_STATES = ("active", "revoked")
# The C03 order the renderer writes allowed_operations in; the digest depends on it.
ALLOWED_OPERATIONS = (
    "collection",
    "immutable_capture",
    "composition",
    "quality_proof_issuance",
    "release_evidence_qualified_staging_results",
)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_GRANT_ID = re.compile(r"[a-z0-9_]+\Z")


def approval_phrase(proposal_sha256: object) -> str:
    """The exact line the approver sends: the prefix plus the proposal file sha256."""
    if not isinstance(proposal_sha256, str) or _HEX_64.fullmatch(proposal_sha256) is None:
        raise ValueError("recurring_grant_phrase_invalid")
    return APPROVAL_PHRASE_PREFIX + proposal_sha256


def approval_phrase_sha256(proposal_sha256: object) -> str:
    return sha256(approval_phrase(proposal_sha256).encode("utf-8")).hexdigest()


def revocation_phrase(grant_id: object) -> str:
    """The exact line that revokes a grant; its sha256 is bound inside the grant."""
    if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
        raise ValueError("recurring_grant_phrase_invalid")
    return REVOCATION_PHRASE_PREFIX + grant_id


def _renderer_instant(value: object) -> bool:
    """The renderer spells an instant as a UTC aware isoformat with a +00:00 offset."""
    if not isinstance(value, str):
        return False
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return False
    return instant.tzinfo is not None and instant.astimezone(UTC).isoformat() == value


def grant_terms(grant: object) -> dict:
    """The exact field set with revocation_state removed: the digested terms.

    Two spellings the approval routine does not pin are pinned here, because the digest
    depends on them: allowed_operations in the C03 order without repeats, and the two
    instants in the renderer's spelling. A document spelled otherwise is refused rather
    than recorded under a digest the integration module never computes.
    """
    if (
        not isinstance(grant, Mapping)
        or tuple(sorted(grant)) != GRANT_FIELDS
        or grant["contract_version"] != GRANT_CONTRACT
        or grant["revocation_state"] not in REVOCATION_STATES
    ):
        raise ValueError("recurring_grant_invalid")
    operations = grant["allowed_operations"]
    if (
        not isinstance(operations, list)
        or not operations
        or operations != [name for name in ALLOWED_OPERATIONS if name in operations]
    ):
        raise ValueError("recurring_grant_invalid")
    if not _renderer_instant(grant["valid_from"]) or not _renderer_instant(grant["valid_until"]):
        raise ValueError("recurring_grant_invalid")
    return {field: grant[field] for field in GRANT_FIELDS if field != "revocation_state"}


def canonical_grant_bytes(grant: object) -> bytes:
    """The bytes the approval store holds as canonical_manifest_json for a grant row."""
    grant_terms(grant)
    return canonical_bytes({field: grant[field] for field in GRANT_FIELDS})


def grant_digest(grant: object) -> str:
    """Canonical digest of the terms; the approval row key (manifest_sha256)."""
    return sha256(canonical_bytes(grant_terms(grant))).hexdigest()
