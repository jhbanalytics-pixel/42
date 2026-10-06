"""Review credentials: verified claims to a pseudonymous role holder, or a refusal.

Signature verification is not exercised here. These tests start from claims the
existing signature verifier has already returned, and prove what this layer adds:
audience, issuer, hosted domain, expiry, an explicit subject allowlist, and a
principal reference derived server side that no request can supply.
"""

from __future__ import annotations

import hashlib

import pytest

from src.api import dossier_approval, dossier_review_auth as auth

AUDIENCE = "review-client.apps.example"
ISSUER = "https://accounts.google.com"
HOSTED_DOMAIN = "reviewers.example"
SUBJECT = "104857309887312345678"
EMAIL = "reviewer@reviewers.example"
NOW = 1_800_000_000
ALLOWLIST = {SUBJECT: "claim_approver"}
# Marks a claim the fixture must leave out, as distinct from one set to None.
ABSENT = object()


def claims(**overrides):
    value = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "hd": HOSTED_DOMAIN,
        "sub": SUBJECT,
        "exp": NOW + 300,
        "iat": NOW - 10,
        "email": EMAIL,
        "email_verified": True,
    }
    value.update(overrides)
    for key, dropped in list(overrides.items()):
        if dropped is ABSENT:
            del value[key]
    return value


def validate(token_claims=ABSENT, **overrides):
    settings = dict(
        now=NOW,
        expected_audience=AUDIENCE,
        expected_issuer=ISSUER,
        expected_hd=HOSTED_DOMAIN,
        subject_allowlist=ALLOWLIST,
    )
    settings.update(overrides)
    return auth.validate_review_credential(
        token_claims=claims() if token_claims is ABSENT else token_claims, **settings
    )


# The credential


def test_a_verified_member_on_the_allowlist_becomes_a_pseudonymous_role_holder():
    principal = validate()
    assert principal == {
        "principal_ref": "human:"
        + hashlib.sha256(SUBJECT.encode("utf-8")).hexdigest()[:8],
        "role": "claim_approver",
    }
    assert dossier_approval.validate_principal(principal["principal_ref"])


def test_the_principal_reference_is_derived_from_the_subject_alone():
    assert auth.principal_ref_for_subject(SUBJECT) == validate()["principal_ref"]
    assert auth.principal_ref_for_subject(SUBJECT) == auth.principal_ref_for_subject(
        SUBJECT
    )
    assert auth.principal_ref_for_subject("1") != auth.principal_ref_for_subject("2")
    same_subject_other_mail = validate(claims(email="other@reviewers.example"))
    assert same_subject_other_mail["principal_ref"] == validate()["principal_ref"]


def test_a_principal_reference_carried_in_the_claims_is_ignored():
    forged = validate(
        claims(principal_ref="human:deadbeef", role="client_read_approver")
    )
    assert forged["principal_ref"] == auth.principal_ref_for_subject(SUBJECT)
    assert forged["role"] == "claim_approver"


@pytest.mark.parametrize(
    ("override", "code", "status", "reason"),
    [
        (
            {"aud": "other-client.apps.example"},
            "review_request_invalid",
            400,
            "audience_mismatch",
        ),
        ({"aud": [AUDIENCE]}, "review_request_invalid", 400, "audience_mismatch"),
        ({"aud": ABSENT}, "review_request_invalid", 400, "audience_mismatch"),
        (
            {"iss": "https://issuer.example"},
            "review_request_invalid",
            400,
            "issuer_mismatch",
        ),
        ({"iss": ABSENT}, "review_request_invalid", 400, "issuer_mismatch"),
        ({"exp": NOW}, "review_request_invalid", 400, "token_expired"),
        ({"exp": NOW - 1}, "review_request_invalid", 400, "token_expired"),
        ({"exp": str(NOW + 300)}, "review_request_invalid", 400, "token_expired"),
        ({"exp": True}, "review_request_invalid", 400, "token_expired"),
        ({"exp": ABSENT}, "review_request_invalid", 400, "token_expired"),
        ({"sub": ABSENT}, "review_request_invalid", 400, "subject_missing"),
        ({"sub": ""}, "review_request_invalid", 400, "subject_missing"),
        (
            {"sub": 104857309887312345678},
            "review_request_invalid",
            400,
            "subject_missing",
        ),
        ({"sub": "x" * 256}, "review_request_invalid", 400, "subject_missing"),
        (
            {"hd": "other.example"},
            "review_role_required",
            403,
            "hosted_domain_mismatch",
        ),
        ({"hd": ABSENT}, "review_role_required", 403, "hosted_domain_mismatch"),
        ({"hd": ""}, "review_role_required", 403, "hosted_domain_mismatch"),
        (
            {"sub": "999999999999999999999"},
            "review_role_required",
            403,
            "subject_not_allowlisted",
        ),
    ],
)
def test_each_claim_failure_is_refused_with_its_package_code(
    override, code, status, reason
):
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(claims(**override))
    assert (caught.value.code, caught.value.status, caught.value.reason) == (
        code,
        status,
        reason,
    )


def test_a_verified_email_on_the_domain_is_not_membership():
    """Email domain or email_verified alone is insufficient. Only a signed hd counts."""
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(claims(hd=ABSENT, email=EMAIL, email_verified=True))
    assert caught.value.code == "review_role_required"


def test_an_allowlisted_subject_whose_role_is_not_a_contract_role_holds_nothing():
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(subject_allowlist={SUBJECT: "superuser"})
    assert (caught.value.code, caught.value.status) == ("review_role_required", 403)
    assert caught.value.reason == "role_unknown"
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(subject_allowlist={SUBJECT: ""})
    assert caught.value.code == "review_role_required"


def test_an_empty_allowlist_closes_review_before_any_claim_is_read():
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(subject_allowlist={})
    assert (caught.value.code, caught.value.status) == (
        "review_authority_unavailable",
        503,
    )
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(token_claims="not claims", subject_allowlist={})
    assert caught.value.code == "review_authority_unavailable"
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(subject_allowlist=[SUBJECT])
    assert caught.value.code == "review_authority_unavailable"


@pytest.mark.parametrize(
    "override",
    [
        {"expected_audience": ""},
        {"expected_issuer": None},
        {"expected_hd": ""},
        {"now": float("nan")},
        {"now": "now"},
    ],
)
def test_a_missing_expectation_closes_review_rather_than_relaxing_it(override):
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(**override)
    assert (caught.value.code, caught.value.status) == (
        "review_authority_unavailable",
        503,
    )


@pytest.mark.parametrize("token_claims", ["header.payload.signature", None, [], 7])
def test_claims_that_are_not_a_mapping_are_a_malformed_request(token_claims):
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(token_claims=token_claims)
    assert (caught.value.code, caught.value.status) == ("review_request_invalid", 400)
    assert caught.value.reason == "claims_malformed"


def test_a_refusal_carries_no_raw_identity():
    with pytest.raises(auth.ReviewAuthError) as caught:
        validate(claims(sub="999999999999999999999", email="leak@reviewers.example"))
    text = " ".join([str(caught.value), caught.value.code, caught.value.reason])
    assert "999999999999999999999" not in text
    assert "leak@" not in text
    assert "reviewers.example" not in text


# The login binding


def bind(**overrides):
    settings = dict(
        nonce="nonce-issued-0001",
        csrf_token="csrf-issued-0001",
        issued_at=NOW,
        now=NOW + 30,
        max_age_seconds=300,
        seen=set(),
        presented_nonce="nonce-issued-0001",
        presented_csrf_token="csrf-issued-0001",
    )
    settings.update(overrides)
    return auth.bind_session(**settings)


def test_a_live_matching_unused_login_binds_to_a_fresh_session():
    seen = set()
    session = bind(seen=seen)
    assert set(session) == {"session_id", "nonce_digest", "bound_at", "expires_at"}
    assert type(session["session_id"]) is str and len(session["session_id"]) >= 32
    assert session["nonce_digest"] == hashlib.sha256(b"nonce-issued-0001").hexdigest()
    assert session["bound_at"] == NOW + 30
    assert session["expires_at"] == NOW + 330
    assert seen == {"nonce-issued-0001"}
    assert "nonce-issued-0001" not in session.values()


def test_two_logins_never_share_a_session_id():
    first = bind()
    second = bind(nonce="nonce-issued-0002", presented_nonce="nonce-issued-0002")
    assert first["session_id"] != second["session_id"]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"now": NOW + 301}, "login_expired"),
        ({"now": NOW - 1}, "login_expired"),
        ({"presented_csrf_token": "csrf-issued-0002"}, "login_mismatch"),
        ({"presented_nonce": "nonce-issued-0002"}, "login_mismatch"),
        ({"presented_nonce": "nonce-issued-0001 "}, "login_mismatch"),
        ({"seen": {"nonce-issued-0001"}}, "login_replayed"),
        ({"nonce": ""}, "login_malformed"),
        ({"csrf_token": None}, "login_malformed"),
        ({"presented_csrf_token": 7}, "login_malformed"),
        ({"presented_nonce": "nönce"}, "login_malformed"),
    ],
)
def test_each_login_failure_is_refused_and_binds_nothing(override, reason):
    seen = override.pop("seen", set())
    before = set(seen)
    with pytest.raises(auth.ReviewAuthError) as caught:
        bind(seen=seen, **override)
    assert (caught.value.code, caught.value.status) == ("review_request_invalid", 400)
    assert caught.value.reason == reason
    assert seen == before


def test_a_login_is_bound_once():
    seen = set()
    bind(seen=seen)
    with pytest.raises(auth.ReviewAuthError) as caught:
        bind(seen=seen)
    assert caught.value.reason == "login_replayed"


@pytest.mark.parametrize(
    "override",
    [
        {"max_age_seconds": 0},
        {"max_age_seconds": True},
        {"max_age_seconds": "300"},
        {"issued_at": float("inf")},
        {"seen": ["nonce"]},
    ],
)
def test_a_broken_login_configuration_closes_the_binding(override):
    with pytest.raises(auth.ReviewAuthError) as caught:
        bind(**override)
    assert (caught.value.code, caught.value.status) == (
        "review_authority_unavailable",
        503,
    )


def test_a_login_refusal_carries_no_presented_value():
    with pytest.raises(auth.ReviewAuthError) as caught:
        bind(presented_csrf_token="csrf-forged-9999")
    text = " ".join([str(caught.value), caught.value.code, caught.value.reason])
    assert "csrf-forged-9999" not in text
    assert "csrf-issued-0001" not in text
