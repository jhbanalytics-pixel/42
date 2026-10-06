"""Immutable recurring execution grant (C03).

The grant binds exactly what C03 lists: issuing principal, executing principals,
resource manifest digest, allowed operations from the C03 list only, supported
source policy and schema versions, approved release manifest digest, run and
monthly allowances, validity instants, retry rules, revocation state, grant id,
permitted cutoffs, reservation per capture and the cumulative ceiling under the
10% headroom rule that ``staging_source_profile`` already enforces for capture
policy. ``validate_recurring_grant`` has one refusal family (``GrantRefusal``),
``grant_digest`` is the canonical digest the daily profile references, and
``check_grant`` is the single admission decision the authority takes before any
reservation. Migration, IAM change, production writes, email, increased
spending and client artifact approval are refused even when a grant lists them.
``proposed_grant`` and ``render_grant_proposal`` produce the proposal document,
so the digest in that document is the digest this module computes.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from hashlib import sha256

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import SUPPORTED_PROFILE_VERSIONS
from src.analysis.open_intelligence.staging_source_profile import CAPTURE_POLICY_HEADROOM_MINIMUM

GRANT_CONTRACT = "42_recurring_execution_grant_v1"
RELEASE_MANIFEST_CONTRACT = "42_recurring_release_manifest_v1"
ALLOWED_OPERATIONS = (
    "collection",
    "immutable_capture",
    "composition",
    "quality_proof_issuance",
    "release_evidence_qualified_staging_results",
)
EXCLUDED_OPERATIONS = (
    "migration",
    "iam_change",
    "production_write",
    "email_send",
    "increased_spending",
    "client_artifact_approval",
)
STAGE_OPERATIONS = {
    "collect": "collection",
    "capture": "immutable_capture",
    "compose": "composition",
    "certify": "quality_proof_issuance",
    "release": "release_evidence_qualified_staging_results",
}
# The origin registry operation each C03 operation stands for, in the grant's stage
# order. The grant, the deployed approve routine and every grant side check keep the
# C03 names; the registry's daily row binds the same five stages under these names.
# This map is the only place the two vocabularies meet: the runner translates through
# it before describe, and the daily authority translates through it when it reserves.
V1_REGISTRY_OPERATIONS = {
    "collection": "collection_exposure_issue",
    "immutable_capture": "source_snapshot_capture",
    "composition": "r3_apply",
    "quality_proof_issuance": "r3_proof_issue",
    "release_evidence_qualified_staging_results": "r3_release",
}
GRANT_HEADROOM_MINIMUM: Fraction = CAPTURE_POLICY_HEADROOM_MINIMUM
RESERVED_MICRO_USD_PER_CAPTURE = 500000
GRANT_VALIDITY_DAYS = 30
PROPOSED_DAILY_POLICY = {
    "cutoffs_per_cycle": 1,
    "max_catchup_cutoffs": 2,
    "freshness_target_hours": 30,
    "max_publish_lag_hours": 48,
}
EXECUTING_IDENTITY_KEYS = (
    "orchestration",
    "ingestion",
    "execution_brain_read",
    "execution_r3_proof_issue",
    "execution_r3_release",
)
GRANT_FIELDS = (
    "contract_version",
    "grant_id",
    "environment",
    "issuing_principal",
    "executing_principals",
    "resource_manifest_digest",
    "allowed_operations",
    "source_policy_digest",
    "schema_version",
    "approved_release_manifest_digest",
    "permitted_image_digests",
    "run_allowance_micro_usd",
    "monthly_allowance_micro_usd",
    "reserved_micro_usd_per_capture",
    "cumulative_ceiling_micro_usd",
    "permitted_cutoffs",
    "valid_from",
    "valid_until",
    "retry_rules",
    "revocation_state",
    "revocation_phrase_sha256",
)
RETRY_RULES = {
    "failed_stage": "retry_safe_only",
    "unknown_or_partial": "hold_for_reconciliation",
}
_DIGEST_FIELDS = (
    "resource_manifest_digest",
    "source_policy_digest",
    "approved_release_manifest_digest",
    "revocation_phrase_sha256",
)
_AMOUNT_FIELDS = (
    "run_allowance_micro_usd",
    "monthly_allowance_micro_usd",
    "reserved_micro_usd_per_capture",
    "cumulative_ceiling_micro_usd",
)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_GRANT_ID = re.compile(r"[a-z0-9_]+\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRINCIPAL = re.compile(r"[a-z][a-z0-9-]*@[a-z0-9-]+\.iam\.gserviceaccount\.com\Z")


class GrantRefusal(ValueError):
    """The one refusal family of the grant contract; ``str(error)`` is the code."""


def _refuse(code: str) -> None:
    raise GrantRefusal(code)


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or unicodedata.normalize("NFC", value) != value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _refuse("recurring_grant_invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        _refuse("recurring_grant_invalid")
    return value


def _amount(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _refuse("recurring_grant_invalid")
    return value


def _instant(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _refuse("recurring_grant_invalid")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse("recurring_grant_invalid")
    return value.astimezone(UTC)


def _cutoff_date(value: object) -> str:
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            _refuse("recurring_grant_invalid")
    except ValueError:
        _refuse("recurring_grant_invalid")
    return value


def _unique_strings(value: object, pattern: re.Pattern[str] | None, *, nonempty: bool) -> list:
    if not isinstance(value, list | tuple) or (nonempty and not value):
        _refuse("recurring_grant_invalid")
    items = []
    for item in value:
        if not isinstance(item, str) or (pattern is not None and pattern.fullmatch(item) is None):
            _refuse("recurring_grant_invalid")
        items.append(item)
    if len(set(items)) != len(items):
        _refuse("recurring_grant_invalid")
    return items


def validate_recurring_grant(grant: object) -> dict:
    """Require an exact, internally consistent grant and return its canonical form."""
    if not isinstance(grant, Mapping) or set(grant) != set(GRANT_FIELDS):
        _refuse("recurring_grant_invalid")
    if grant["contract_version"] != GRANT_CONTRACT or grant["environment"] != "staging":
        _refuse("recurring_grant_invalid")
    grant_id = grant["grant_id"]
    if not isinstance(grant_id, str) or _GRANT_ID.fullmatch(grant_id) is None:
        _refuse("recurring_grant_invalid")
    if grant["schema_version"] not in SUPPORTED_PROFILE_VERSIONS:
        _refuse("recurring_grant_invalid")
    if grant["revocation_state"] not in {"active", "revoked"}:
        _refuse("recurring_grant_invalid")
    operations = _unique_strings(grant["allowed_operations"], None, nonempty=True)
    if any(operation in EXCLUDED_OPERATIONS for operation in operations):
        _refuse("recurring_grant_operation_excluded")
    if any(operation not in ALLOWED_OPERATIONS for operation in operations):
        _refuse("recurring_grant_invalid")
    rules = grant["retry_rules"]
    if (
        not isinstance(rules, Mapping)
        or set(rules) != {*RETRY_RULES, "max_attempt_versions"}
        or any(rules[name] != expected for name, expected in RETRY_RULES.items())
        or isinstance(rules["max_attempt_versions"], bool)
        or not isinstance(rules["max_attempt_versions"], int)
        or rules["max_attempt_versions"] < 2
    ):
        _refuse("recurring_grant_invalid")
    checked: dict = {
        "contract_version": GRANT_CONTRACT,
        "grant_id": grant_id,
        "environment": "staging",
        "issuing_principal": _text(grant["issuing_principal"]),
        "executing_principals": sorted(
            _unique_strings(grant["executing_principals"], _PRINCIPAL, nonempty=True)
        ),
        "allowed_operations": [op for op in ALLOWED_OPERATIONS if op in operations],
        "schema_version": grant["schema_version"],
        "permitted_image_digests": sorted(
            _unique_strings(grant["permitted_image_digests"], _IMAGE_DIGEST, nonempty=False)
        ),
        "retry_rules": {**RETRY_RULES, "max_attempt_versions": rules["max_attempt_versions"]},
        "revocation_state": grant["revocation_state"],
    }
    for field in _DIGEST_FIELDS:
        checked[field] = _digest(grant[field])
    for field in _AMOUNT_FIELDS:
        checked[field] = _amount(grant[field])
    valid_from = _instant(grant["valid_from"])
    valid_until = _instant(grant["valid_until"])
    if valid_until <= valid_from:
        _refuse("recurring_grant_invalid")
    cutoffs = sorted(
        _cutoff_date(item)
        for item in _unique_strings(grant["permitted_cutoffs"], None, nonempty=True)
    )
    for item in cutoffs:
        instant = datetime.combine(date.fromisoformat(item), datetime.min.time(), tzinfo=UTC)
        if not valid_from <= instant < valid_until:
            _refuse("recurring_grant_cutoff_forbidden")
    reserved = checked["reserved_micro_usd_per_capture"]
    headroom = Fraction(reserved - checked["run_allowance_micro_usd"], reserved)
    if headroom < GRANT_HEADROOM_MINIMUM:
        _refuse("recurring_grant_headroom_insufficient")
    if checked["cumulative_ceiling_micro_usd"] < reserved * len(cutoffs):
        _refuse("recurring_grant_ceiling_insufficient")
    if checked["monthly_allowance_micro_usd"] < checked["cumulative_ceiling_micro_usd"]:
        _refuse("recurring_grant_allowance_insufficient")
    checked["permitted_cutoffs"] = cutoffs
    checked["valid_from"] = valid_from.isoformat()
    checked["valid_until"] = valid_until.isoformat()
    return {field: checked[field] for field in GRANT_FIELDS}


def grant_digest(grant: object) -> str:
    """Canonical digest of the immutable terms; revocation is a state on top of them."""
    checked = validate_recurring_grant(grant)
    terms = {field: value for field, value in checked.items() if field != "revocation_state"}
    return sha256(canonical_bytes(terms)).hexdigest()


def check_grant(
    grant: object,
    *,
    now: datetime,
    operation: str,
    principal: str,
    manifest_digest: str,
    source_policy_digest: str,
    image_digest: str,
    cutoff_utc: datetime | None = None,
) -> dict:
    """Admit exactly one operation under the grant or refuse with one family of codes."""
    checked = validate_recurring_grant(grant)
    current = _instant(now)
    if operation in EXCLUDED_OPERATIONS:
        _refuse("recurring_grant_operation_excluded")
    if checked["revocation_state"] != "active":
        _refuse("recurring_grant_revoked")
    if current < datetime.fromisoformat(checked["valid_from"]):
        _refuse("recurring_grant_not_yet_valid")
    if current >= datetime.fromisoformat(checked["valid_until"]):
        _refuse("recurring_grant_expired")
    if principal not in checked["executing_principals"]:
        _refuse("recurring_grant_principal_mismatch")
    if manifest_digest != checked["resource_manifest_digest"]:
        _refuse("recurring_grant_manifest_mismatch")
    if source_policy_digest != checked["source_policy_digest"]:
        _refuse("recurring_grant_source_policy_unsupported")
    if operation not in checked["allowed_operations"]:
        _refuse("recurring_grant_operation_forbidden")
    if image_digest not in checked["permitted_image_digests"]:
        _refuse("recurring_grant_image_forbidden")
    cutoff = None
    if cutoff_utc is not None:
        instant = _instant(cutoff_utc)
        cutoff = instant.date().isoformat()
        if instant != datetime.combine(instant.date(), datetime.min.time(), tzinfo=UTC):
            _refuse("recurring_grant_cutoff_forbidden")
        if cutoff not in checked["permitted_cutoffs"]:
            _refuse("recurring_grant_cutoff_forbidden")
    return {
        "grant_id": checked["grant_id"],
        "grant_digest": grant_digest(checked),
        "operation": operation,
        "principal": principal,
        "cutoff": cutoff,
    }


def capture_grant_from(grant: object) -> dict:
    """Project the capture reservation terms in the shape ``validate_capture_grant`` reads."""
    checked = validate_recurring_grant(grant)
    return {
        "grant_id": checked["grant_id"],
        "environment": "staging",
        "source_estate_digest": checked["source_policy_digest"],
        "contract_sha256": grant_digest(checked),
        "valid_from": checked["valid_from"],
        "valid_until": checked["valid_until"],
        "allowed_cutoffs": list(checked["permitted_cutoffs"]),
        "reserved_micro_usd_per_capture": checked["reserved_micro_usd_per_capture"],
        "cumulative_ceiling_micro_usd": checked["cumulative_ceiling_micro_usd"],
        "revocation_state": checked["revocation_state"],
    }


def proposed_release_manifest() -> dict:
    """The release manifest the proposal binds: release stays with the human contract."""
    return {
        "contract_version": RELEASE_MANIFEST_CONTRACT,
        "delegation": "none",
        "human_release_contract": "42_release_authority_v1",
    }


def _identity_email(identities: Mapping[str, object], key: str) -> str:
    value = identities.get(key)
    if not isinstance(value, str) or "/serviceAccounts/" not in value:
        _refuse("recurring_grant_invalid")
    return value.rsplit("/", 1)[1]


def proposed_grant(
    *,
    identities: Mapping[str, object],
    issuing_principal: str,
    resource_manifest_digest: str,
    source_policy_digest: str,
    valid_from: datetime,
    permitted_image_digests: tuple[str, ...] | list[str] = (),
) -> dict:
    """Build the C03 proposal grant from the reviewed manifest and the tree's allowances.

    Validity starts at 00:00:00 UTC on the approval day, so the first permitted cutoff is
    inside the window whatever the approval instant.
    """
    start = _instant(valid_from).replace(hour=0, minute=0, second=0, microsecond=0)
    cutoffs = [
        (start + timedelta(days=offset)).date().isoformat() for offset in range(GRANT_VALIDITY_DAYS)
    ]
    reserved = RESERVED_MICRO_USD_PER_CAPTURE
    headroom = math.ceil(reserved * GRANT_HEADROOM_MINIMUM)
    grant_id = f"recurring_execution_{start.date().isoformat().replace('-', '')}_v1"
    revocation_phrase = f"Revoke recurring execution grant {grant_id}"
    return validate_recurring_grant(
        {
            "contract_version": GRANT_CONTRACT,
            "grant_id": grant_id,
            "environment": "staging",
            "issuing_principal": issuing_principal,
            # The keys record which operations the grant covers; two keys naming one
            # account (the daily operations since amendment d) yield one principal.
            "executing_principals": sorted(
                {_identity_email(identities, key) for key in EXECUTING_IDENTITY_KEYS}
            ),
            "resource_manifest_digest": resource_manifest_digest,
            "allowed_operations": list(ALLOWED_OPERATIONS),
            "source_policy_digest": source_policy_digest,
            "schema_version": "42_daily_v1",
            "approved_release_manifest_digest": sha256(
                canonical_bytes(proposed_release_manifest())
            ).hexdigest(),
            "permitted_image_digests": list(permitted_image_digests),
            "run_allowance_micro_usd": reserved - headroom,
            "monthly_allowance_micro_usd": reserved * len(cutoffs),
            "reserved_micro_usd_per_capture": reserved,
            "cumulative_ceiling_micro_usd": reserved * len(cutoffs),
            "permitted_cutoffs": cutoffs,
            "valid_from": start.isoformat(),
            "valid_until": (start + timedelta(days=GRANT_VALIDITY_DAYS)).isoformat(),
            "retry_rules": {**RETRY_RULES, "max_attempt_versions": 3},
            "revocation_state": "active",
            "revocation_phrase_sha256": sha256(revocation_phrase.encode("utf-8")).hexdigest(),
        }
    )


def _usd(micro: int) -> str:
    return f"{micro} microUSD ({micro / 1_000_000:.2f} USD)"


def render_grant_proposal(grant: object) -> str:
    """Render the proposal document for the issuing principal; the digest is this module's."""
    checked = validate_recurring_grant(grant)
    digest = grant_digest(checked)
    policy = PROPOSED_DAILY_POLICY
    cutoffs = checked["permitted_cutoffs"]
    images = ", ".join(checked["permitted_image_digests"]) or "none yet"
    principals = "\n".join(f"- {principal}" for principal in checked["executing_principals"])
    operations = "\n".join(f"- {operation}" for operation in checked["allowed_operations"])
    revocation = f'"Revoke recurring execution grant {checked["grant_id"]}"'
    paragraphs = [
        "# Recurring execution grant proposal",
        f"Grant digest: {digest}",
        f"Contract {GRANT_CONTRACT}, grant id {checked['grant_id']}, environment staging. "
        "This document is rendered from the grant itself, so the digest above is the canonical "
        "digest the daily profile references. Approval covers the sha256 of this file; a change "
        "to any value below changes both digests and needs a fresh approval. Nothing here is "
        "inherited authority, and no paid stage runs before approval. A scheduled firing stops "
        "at grant_missing until an approval record carrying the approval phrase digest exists; "
        "under the shipped wiring, which has no stage adapters yet, it stops at "
        "stage_adapter_unavailable; with adapters present it stops at operation_unbound until "
        "the origin registry binds the five operations for the active generation; once an "
        "image digest is rendered into this grant it stops at "
        "recurring_grant_image_forbidden until that rendering is approved; after approval the "
        "paid stages run and the cycle stops at release_pending until the human release "
        "contract or an approved recurring delegation covers release.",
        "## Principals",
        f"Issuing principal: {checked['issuing_principal']}.",
        "Executing principals, taken from the identities block of the reviewed resource manifest "
        f"whose digest is {checked['resource_manifest_digest']}:",
        principals,
        "## Allowed operations",
        "The grant admits these operations and no other:",
        operations,
        "Migration, IAM change, production writes, email sends, increased spending and client "
        "artifact approval are excluded by the contract and refused even if a later grant were "
        "to list them.",
        "## Bound versions",
        f"Source policy digest {checked['source_policy_digest']}, the reconciled source estate "
        f"the collection stage may touch. Profile schema version {checked['schema_version']}. "
        f"Approved release manifest digest {checked['approved_release_manifest_digest']}, which "
        "declares no recurring release delegation: the release stage keeps waiting for the "
        "existing human release contract and returns release_pending.",
        f"Permitted image digests: {images}. An image outside this list stops before dispatch. "
        "The list is empty in this rendering because the staging image that will run these "
        "stages is not built yet; the approval rendering carries that digest and therefore a "
        "new grant digest.",
        "## Allowances",
        "The amounts come from the capture allowance policy in the tree, in "
        "engine/src/analysis/open_intelligence/staging_source_profile.py: the legacy policy "
        "reserves 500000 microUSD per capture (_LEGACY_POLICY, reservation block), the grant "
        "policy keeps that reservation per cutoff (_grant_policy), and "
        "CAPTURE_POLICY_HEADROOM_MINIMUM fixes the 10% headroom that "
        "maximum_cycle_cost_bound_micro_usd is derived from.",
        f"Reservation per capture: {_usd(checked['reserved_micro_usd_per_capture'])}. Run "
        f"allowance, the modeled maximum per cycle: {_usd(checked['run_allowance_micro_usd'])}, "
        "which keeps at least 10% of the reservation as headroom. Cumulative ceiling: "
        f"{_usd(checked['cumulative_ceiling_micro_usd'])}, the reservation across all "
        f"{len(cutoffs)} permitted cutoffs. Monthly allowance: "
        f"{_usd(checked['monthly_allowance_micro_usd'])}.",
        "## Validity and cutoffs",
        f"Valid from {checked['valid_from']} to {checked['valid_until']}, thirty days from "
        "00:00:00 UTC on the approval day. If approval lands on a later day the grant is "
        "re-rendered from 00:00:00 UTC on that day and the digest changes with it.",
        f"Permitted cutoffs: {cutoffs[0]} through {cutoffs[-1]}, one per UTC day, each naming "
        "the closed observation window that ends at 00:00:00 UTC on that date.",
        "## Proposed daily policy",
        f"One closed UTC cutoff per cycle ({policy['cutoffs_per_cycle']}). At most "
        f"{policy['max_catchup_cutoffs']} missed cutoffs caught up per activation. Freshness "
        f"target {policy['freshness_target_hours']} hours after the cutoff. Maximum normal "
        f"publication lag {policy['max_publish_lag_hours']} hours. These are proposed "
        "operational values for approval, not inherited authority, and they never loosen an "
        "existing tighter requirement.",
        "## Retry rules and revocation",
        "A failed stage is retried only when its durable result is proven retry safe. Unknown "
        "or partial paid work holds for reconciliation and never repeats a whole stage. At most "
        f"{checked['retry_rules']['max_attempt_versions']} attempt versions exist per unit.",
        f"Revocation is by phrase. Sending the exact line {revocation} revokes the grant; its "
        f"sha256 is bound as {checked['revocation_phrase_sha256']}. Revocation prevents the "
        "next operation while known in-flight state is reconciled.",
        "## Approval",
        "Approve recurring execution grant followed by the sha256 of this file.",
    ]
    return "\n\n".join(paragraphs) + "\n"
