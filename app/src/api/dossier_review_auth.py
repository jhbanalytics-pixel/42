"""Review credentials after signature verification, and the login binding.

This layer starts where the signature verifier stops. The existing worker
verifier already owns the step that proves a token was signed by the identity
provider, and that step is not repeated or replaced here: the caller hands in
the claims that verifier returned. What this layer decides is whether those
claims name a reviewer this deployment admits. The audience, issuer and hosted
domain must each equal the value the deployment expects, the token must not
have expired, and the subject must appear in an explicit allowlist that maps a
stable subject to one contract role, or to a list of distinct contract roles
that one person holds together. Organizational membership is the
signed hosted domain and nothing weaker: an email on the domain, verified or
not, is not membership.

The principal reference a decision records is derived here and nowhere else.
It is the text "human:" followed by the first eight hexadecimal characters of
the SHA256 digest of the subject encoded as UTF-8. The subject is the identity
provider's stable identifier for the account, so the reference is stable for
one account and carries no email, no name and no token. A reference supplied by
a request, in a claim or in a command, is never read.

A login is bound to a session once. The server issues a nonce and a CSRF token
when the login starts; when the credential comes back, the nonce inside it and
the CSRF token posted with it must equal the issued pair, the login must still
be inside its window, and the nonce must not have been bound before. The
caller keeps the set of bound nonces. Command idempotency is a separate
mechanism in the review store and does not stand in for this one.

Every refusal is typed and carries a package code, a status and a short reason
that names the check, never the value that failed it.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from collections.abc import Mapping, MutableSet

from . import dossier_approval

__all__ = [
    "AUTHORITY_UNAVAILABLE",
    "REQUEST_INVALID",
    "ROLE_REQUIRED",
    "ReviewAuthError",
    "bind_session",
    "principal_ref_for_subject",
    "validate_review_credential",
]

REQUEST_INVALID = "review_request_invalid"
ROLE_REQUIRED = "review_role_required"
AUTHORITY_UNAVAILABLE = "review_authority_unavailable"

_STATUS = {REQUEST_INVALID: 400, ROLE_REQUIRED: 403, AUTHORITY_UNAVAILABLE: 503}
_SUBJECT_LIMIT = 255
_LOGIN_TEXT_LIMIT = 512


class ReviewAuthError(ValueError):
    """A controlled refusal that names the check and never the value."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = _STATUS[code]
        self.reason = reason


def _refuse(code: str, reason: str) -> None:
    raise ReviewAuthError(code, reason) from None


def _finite(value: object) -> bool:
    return (
        type(value) in (int, float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _text(value: object, limit: int) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= limit
        and value.isascii()
        and value.isprintable()
    )


def principal_ref_for_subject(subject: str) -> str:
    """The pseudonymous reference for one subject: human: plus eight hex of SHA256."""
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    return dossier_approval.validate_principal("human:" + digest[:8])


def _listed_roles(value: object) -> list[str] | None:
    """The roles one allowlist entry grants, in the order listed, or None when malformed."""
    if type(value) is str:
        values = [value]
    elif type(value) is list and value:
        values = value
    else:
        return None
    if not all(
        type(role) is str and role in dossier_approval.APPROVAL_ROLES for role in values
    ) or len(set(values)) != len(values):
        return None
    return list(values)


def validate_review_credential(
    *,
    token_claims: object,
    now: object,
    expected_audience: object,
    expected_issuer: object,
    expected_hd: object,
    subject_allowlist: Mapping[str, str | list[str]],
    enrolment: bool = False,
) -> dict:
    """Verified claims to a role holder, or a refusal that reveals nothing.

    The deployment's own expectations are checked first. A missing expectation
    closes review for everyone rather than admitting anyone, and does so before
    a single claim is read. An empty allowlist closes review the same way
    unless enrolment is asked for: then every claim is still checked and every
    subject is refused as not allowlisted, so the caller can record who tried.

    A subject listed with one role string holds that role, as it always has.
    A subject listed with a list of distinct roles holds all of them: role is
    the first, for callers that read one, and roles is the whole list.
    """
    if (
        not _text(expected_audience, _LOGIN_TEXT_LIMIT)
        or not _text(expected_issuer, _LOGIN_TEXT_LIMIT)
        or not _text(expected_hd, _LOGIN_TEXT_LIMIT)
        or not _finite(now)
    ):
        _refuse(AUTHORITY_UNAVAILABLE, "configuration_missing")
    if not isinstance(subject_allowlist, Mapping):
        _refuse(AUTHORITY_UNAVAILABLE, "configuration_missing")
    if len(subject_allowlist) == 0 and enrolment is not True:
        _refuse(AUTHORITY_UNAVAILABLE, "allowlist_empty")
    if type(token_claims) is not dict:
        _refuse(REQUEST_INVALID, "claims_malformed")
    if token_claims.get("aud") != expected_audience:
        _refuse(REQUEST_INVALID, "audience_mismatch")
    if token_claims.get("iss") != expected_issuer:
        _refuse(REQUEST_INVALID, "issuer_mismatch")
    expires = token_claims.get("exp")
    if type(expires) is not int or not expires > now:
        _refuse(REQUEST_INVALID, "token_expired")
    subject = token_claims.get("sub")
    if not _text(subject, _SUBJECT_LIMIT):
        _refuse(REQUEST_INVALID, "subject_missing")
    hosted_domain = token_claims.get("hd")
    if type(hosted_domain) is not str or not hmac.compare_digest(
        hosted_domain.encode("utf-8"), expected_hd.encode("utf-8")
    ):
        _refuse(ROLE_REQUIRED, "hosted_domain_mismatch")
    listed = subject_allowlist.get(subject)
    if listed is None:
        _refuse(ROLE_REQUIRED, "subject_not_allowlisted")
    roles = _listed_roles(listed)
    if roles is None:
        _refuse(ROLE_REQUIRED, "role_unknown")
    principal = {"principal_ref": principal_ref_for_subject(subject), "role": roles[0]}
    if type(listed) is list:
        principal["roles"] = roles
    return principal


def bind_session(
    *,
    nonce: str,
    csrf_token: str,
    issued_at: object,
    now: object,
    max_age_seconds: int,
    seen: MutableSet[str],
    presented_nonce: object,
    presented_csrf_token: object,
) -> dict:
    """Bind one issued login to one short-lived session, once.

    nonce, csrf_token and issued_at are what the server issued when the login
    started. presented_nonce is the nonce claim inside the returned credential
    and presented_csrf_token is the token posted back with it. The session
    lives as long as the login window did, and the record keeps a digest of
    the nonce rather than the nonce, so a session never carries a login value.
    """
    if (
        not _finite(issued_at)
        or not _finite(now)
        or type(max_age_seconds) is not int
        or max_age_seconds <= 0
        or not isinstance(seen, MutableSet)
    ):
        _refuse(AUTHORITY_UNAVAILABLE, "configuration_missing")
    if not all(
        _text(value, _LOGIN_TEXT_LIMIT)
        for value in (nonce, csrf_token, presented_nonce, presented_csrf_token)
    ):
        _refuse(REQUEST_INVALID, "login_malformed")
    if not issued_at <= now <= issued_at + max_age_seconds:
        _refuse(REQUEST_INVALID, "login_expired")
    nonce_matches = hmac.compare_digest(
        nonce.encode("utf-8"), presented_nonce.encode("utf-8")
    )
    csrf_matches = hmac.compare_digest(
        csrf_token.encode("utf-8"), presented_csrf_token.encode("utf-8")
    )
    if not (nonce_matches and csrf_matches):
        _refuse(REQUEST_INVALID, "login_mismatch")
    if nonce in seen:
        _refuse(REQUEST_INVALID, "login_replayed")
    seen.add(nonce)
    return {
        "session_id": secrets.token_urlsafe(32),
        "nonce_digest": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        "bound_at": now,
        "expires_at": now + max_age_seconds,
    }
