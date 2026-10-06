"""Verify signed synthetic JWTs through the actual Google verifier and HTTP adapter."""

import importlib
import io
import json
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
EMAIL = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"


def module():
    return importlib.import_module("src.api.general_question_oidc")


@pytest.fixture(scope="module")
def signing():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "offline synthetic verifier")]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return crypt.RSASigner.from_string(
        private, key_id="synthetic"
    ), certificate.public_bytes(serialization.Encoding.PEM).decode()


def token(signing, **changes):
    now = int(time.time())
    payload = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": EMAIL,
        "email_verified": True,
        "sub": "123456789",
        "iat": now - 5,
        "exp": now + 300,
    }
    payload.update(changes)
    return jwt.encode(signing[0], payload).decode()


def intercept(monkeypatch, signing, mode="success"):
    sent = []

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append((request, kwargs))
            assert request.url == "https://www.googleapis.com/oauth2/v1/certs"
            assert "Authorization" not in request.headers
            if mode == "timeout":
                raise requests.Timeout("synthetic transport failed")
            response = requests.Response()
            response.status_code = 307 if mode == "redirect" else 200
            response.request = request
            response.url = request.url
            if mode == "redirect":
                response.headers["Location"] = "https://evil.invalid/cert"
            body = json.dumps({"synthetic": signing[1]}).encode()
            if mode == "oversized":
                body = b" " * 262145
            response.raw = io.BytesIO(body)
            return response

        def close(self):
            pass

    original = requests.Session.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.mount("https://", Adapter())

    monkeypatch.setattr(requests.Session, "__init__", initialize)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *args: pytest.fail("network forbidden")
    )
    return sent


def verify(header, **kwargs):
    return module().verify_question_worker_authorization(
        header, deadline=kwargs.get("deadline", time.monotonic() + 10)
    )


def test_actual_signature_and_claims_bind_worker_identity(monkeypatch, signing):
    sent = intercept(monkeypatch, signing)
    value = verify(["Bearer " + token(signing)])
    assert value["email"] == EMAIL
    assert value["audience"] == AUDIENCE
    assert value["subject"] == "123456789"
    assert set(value) == {"email", "audience", "subject", "expires_at"}
    assert len(sent) == 1
    assert sent[0][0].method == "GET"
    assert 0 < sent[0][1]["timeout"] <= 5


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://evil.invalid"},
        {"aud": AUDIENCE + "/"},
        {"email": "other@example.com"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"exp": 1},
        {"exp": True},
        {"iat": "0"},
        {"iat": 9999999999},
        {"sub": ""},
        {"aud": [AUDIENCE]},
    ],
)
def test_verified_but_wrong_claims_refuse_safely(monkeypatch, signing, changes):
    intercept(monkeypatch, signing)
    encoded = token(signing, **changes)
    with pytest.raises(module().WorkerOIDCError) as raised:
        verify(["Bearer " + encoded])
    assert raised.value.status_code in (401, 403)
    assert encoded not in str(raised.value)
    assert raised.value.__suppress_context__


def test_changed_signature_is_not_decode_only_authentication(monkeypatch, signing):
    intercept(monkeypatch, signing)
    parts = token(signing).split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    with pytest.raises(module().WorkerOIDCError):
        verify(["Bearer " + ".".join(parts)])


@pytest.mark.parametrize(
    "headers",
    [
        [],
        ["passcode"],
        ["Bearer malformed"],
        ["Bearer a.b.c\r\nX: y"],
        ["Bearer a.b.c", "Bearer a.b.c"],
        None,
    ],
)
def test_invalid_authorization_headers_do_not_fetch_certificates(
    monkeypatch, signing, headers
):
    sent = intercept(monkeypatch, signing)
    with pytest.raises(module().WorkerOIDCError):
        verify(headers)
    assert sent == []


@pytest.mark.parametrize("mode", ["timeout", "redirect", "oversized"])
def test_certificate_transport_is_bounded_and_never_redirects(
    monkeypatch, signing, mode
):
    sent = intercept(monkeypatch, signing, mode)
    with pytest.raises(module().WorkerOIDCError) as raised:
        verify(["Bearer " + token(signing)])
    assert raised.value.status_code == 503
    assert len(sent) == 1


def test_expired_auth_deadline_makes_no_request(monkeypatch, signing):
    sent = intercept(monkeypatch, signing)
    with pytest.raises(module().WorkerOIDCError):
        verify(["Bearer " + token(signing)], deadline=time.monotonic() - 1)
    assert sent == []


def test_expiry_rechecked_after_signature_verification(monkeypatch, signing):
    intercept(monkeypatch, signing)
    encoded = token(signing)
    original = module().id_token.verify_oauth2_token

    def verified_then_expired(*args, **kwargs):
        claims = original(*args, **kwargs)
        monkeypatch.setattr(module().time, "time", lambda: claims["exp"])
        return claims

    monkeypatch.setattr(module().id_token, "verify_oauth2_token", verified_then_expired)
    with pytest.raises(module().WorkerOIDCError):
        verify(["Bearer " + encoded])


def test_slow_certificate_read_expires_without_second_request(monkeypatch, signing):
    sent = intercept(monkeypatch, signing)
    clock = [10.0]
    monkeypatch.setattr(module().time, "monotonic", lambda: clock[0])
    original = requests.Session.get

    def delayed(self, *args, **kwargs):
        response = original(self, *args, **kwargs)
        clock[0] = 21.0
        return response

    monkeypatch.setattr(requests.Session, "get", delayed)
    with pytest.raises(module().WorkerOIDCError) as raised:
        verify(["Bearer " + token(signing)], deadline=20.0)
    assert raised.value.status_code == 503
    assert len(sent) == 1


def test_missing_required_verified_claim_refuses(monkeypatch, signing):
    intercept(monkeypatch, signing)
    now = int(time.time())
    encoded = jwt.encode(
        signing[0],
        {
            "aud": AUDIENCE,
            "email": EMAIL,
            "email_verified": True,
            "sub": "123",
            "iat": now - 5,
            "exp": now + 300,
        },
    ).decode()
    with pytest.raises(module().WorkerOIDCError):
        verify(["Bearer " + encoded])
