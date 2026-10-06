import base64
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi.testclient import TestClient
from google.auth.exceptions import TransportError
from starlette.requests import Request

import core.api
from core.api import app as api_mod
from core.api import auth

AUDIENCE = "/projects/123456789/locations/us-central1/services/f42-api"
EMAIL = "pilot@example.org"
PASSCODE = "existing-passcode"
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(private_key, claims=None, kid="pilot-key"):
    now = int(time.time())
    header = {"alg": "ES256", "kid": kid, "typ": "JWT"}
    payload = {
        "aud": AUDIENCE,
        "email": EMAIL,
        "exp": now + 300,
        "iat": now - 30,
        "iss": "https://cloud.google.com/iap",
    }
    payload.update(claims or {})
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64(signature)}"


def _request(method, headers):
    path = b"/api/today"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path.decode("ascii"),
        "raw_path": path,
        "query_string": b"",
        "headers": [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()],
        "server": ("test", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    return Request(scope)


@pytest.fixture
def iap_client(monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    fetches = []
    certificates = {"pilot-key": public_pem}
    for index in range(4):
        rotated_key = ec.generate_private_key(ec.SECP256R1())
        certificates[f"rotated-key-{index}"] = rotated_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    class CertResponse:
        status = 200
        data = json.dumps(certificates).encode("utf-8")

    def install_cert_fetcher(fetch=None):
        class MockRequest:
            def __call__(self, url, method="GET", timeout=None, **kwargs):
                fetches.append((url, method, timeout))
                if fetch:
                    return fetch(url, timeout)
                return CertResponse()

        monkeypatch.setattr(auth, "GoogleAuthRequest", lambda: MockRequest())

    monkeypatch.setenv("F42_AUTH_MODE", "iap_readonly")
    monkeypatch.setenv("IAP_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("IAP_ALLOWED_EMAILS", EMAIL)
    monkeypatch.setenv("UI_PASSCODE", PASSCODE)
    monkeypatch.setattr(
        core.api,
        "store",
        SimpleNamespace(get_store=lambda: SimpleNamespace(health=lambda: "ok")),
        raising=False,
    )
    monkeypatch.setattr(
        core.api,
        "today",
        SimpleNamespace(
            build_today=lambda store, date=None: {"date": date, "ok": True},
            today_status=lambda store, date: "published",
        ),
        raising=False,
    )

    async def agent_check():
        return "ok"

    monkeypatch.setattr(api_mod, "_agent_check", agent_check)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    install_cert_fetcher()
    return SimpleNamespace(
        client=TestClient(api_mod.app),
        private_key=private_key,
        fetches=fetches,
        install_cert_fetcher=install_cert_fetcher,
    )


def test_iap_auth_dependency_accepts_get_and_head_policy(iap_client):
    assertion = _token(iap_client.private_key)
    headers = {ASSERTION_HEADER: assertion}

    response = iap_client.client.get("/api/today", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"date": None, "ok": True}
    assert iap_client.client.head("/api/today", headers=headers).status_code == 404
    auth.require_passcode(_request("HEAD", headers))
    assert iap_client.fetches
    assert all(
        url == CERTS_URL and method == "GET" and timeout == 5
        for url, method, timeout in iap_client.fetches
    )
    health = iap_client.client.get("/api/health")
    assert health.json()["auth_mode"] == "iap_readonly"
    assert health.json()["passcode"] is False
    assert health.json()["checks"]["auth"] == "ok"
    assert AUDIENCE not in health.text
    assert EMAIL not in health.text


def test_iap_assertion_rejects_invalid_claims_and_signatures(iap_client):
    cases = [
        ("audience", _token(iap_client.private_key, {"aud": "/wrong/audience"})),
        ("issuer", _token(iap_client.private_key, {"iss": "https://accounts.google.com"})),
        ("expired", _token(iap_client.private_key, {"exp": int(time.time()) - 60})),
        ("future", _token(iap_client.private_key, {"iat": int(time.time()) + 60})),
        ("lifetime", _token(iap_client.private_key, {"exp": int(time.time()) + 1200})),
        ("nonfinite", _token(iap_client.private_key, {"exp": float("nan")})),
        ("unknown key", _token(iap_client.private_key, kid="unknown-key")),
    ]
    valid_parts = _token(iap_client.private_key).split(".")
    valid_parts[2] = _b64(b"\x01" * 64)
    cases.append(("signature", ".".join(valid_parts)))

    for name, assertion in cases:
        response = iap_client.client.get(
            "/api/today", headers={ASSERTION_HEADER: assertion}
        )
        assert response.status_code == 401, name
        assert response.json()["error"] == "unauthorized", name
    assert iap_client.fetches, "signed assertions must use the fixed IAP certificates"


def test_iap_rejects_malformed_assertion(iap_client):
    response = iap_client.client.get(
        "/api/today", headers={ASSERTION_HEADER: "not-a-jwt"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_iap_assertion_rejects_unlisted_identity(iap_client):
    assertion = _token(iap_client.private_key, {"email": "other@example.org"})
    response = iap_client.client.get(
        "/api/today", headers={ASSERTION_HEADER: assertion}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert iap_client.fetches, "the signed identity must come from the verified JWT"


@pytest.mark.parametrize(
    "bad_response",
    [
        "empty_map",
        "non_mapping",
        "non_string_value",
        "invalid_pem",
        "invalid_curve",
        "bad_json",
        "bad_utf8",
    ],
)
def test_iap_invalid_certificate_response_is_unavailable(iap_client, bad_response):
    if bad_response == "empty_map":
        data = b"{}"
    elif bad_response == "non_mapping":
        data = b"[]"
    elif bad_response == "non_string_value":
        data = b'{"pilot-key": 7}'
    elif bad_response == "invalid_pem":
        data = b'{"pilot-key": "not a PEM key"}'
    elif bad_response == "invalid_curve":
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = rsa_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        data = json.dumps({"pilot-key": pem}).encode("utf-8")
    elif bad_response == "bad_json":
        data = b"{bad json"
    else:
        data = b"\xff"

    def malformed_response(url, timeout):
        return SimpleNamespace(status=200, data=data)

    iap_client.install_cert_fetcher(malformed_response)
    response = iap_client.client.get(
        "/api/today",
        headers={ASSERTION_HEADER: _token(iap_client.private_key)},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": "auth_unavailable",
        "message": "Authentication is unavailable. Try again shortly.",
    }


def test_iap_rejects_passcode_and_forwarded_email_without_assertion(iap_client):
    headers = {
        "X-Passcode": PASSCODE,
        "X-Goog-Authenticated-User-Email": EMAIL,
        "X-Forwarded-Email": EMAIL,
    }
    response = iap_client.client.get("/api/today", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"

    response = iap_client.client.post(
        "/api/auth/verify", json={"passcode": PASSCODE}, headers=headers
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_iap_auth_verify_ignores_passcode_body(iap_client):
    response = iap_client.client.post(
        "/api/auth/verify",
        json={"passcode": "wrong"},
        headers={ASSERTION_HEADER: _token(iap_client.private_key)},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_iap_write_guard_runs_before_limiter_and_handler(iap_client, monkeypatch):
    calls = []

    def counted_limiter(request, count=True):
        calls.append("limiter")

    async def counted_forward(*args, **kwargs):
        calls.append("handler")
        raise AssertionError("read-only guard must run before the handler")

    monkeypatch.setattr(auth.ask_limiter, "check", counted_limiter)
    monkeypatch.setattr(api_mod, "_forward", counted_forward)
    response = iap_client.client.post(
        "/api/watches",
        json={"market": "ZA"},
        headers={ASSERTION_HEADER: _token(iap_client.private_key)},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "read_only_pilot"
    assert calls == []


def test_iap_certificate_transport_failure_is_fixed_and_closed(iap_client):
    def unavailable(url, timeout):
        raise TransportError("private transport detail")

    iap_client.install_cert_fetcher(unavailable)
    response = iap_client.client.get(
        "/api/today",
        headers={ASSERTION_HEADER: _token(iap_client.private_key)},
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": "auth_unavailable",
        "message": "Authentication is unavailable. Try again shortly.",
    }
    assert "private transport detail" not in response.text


@pytest.mark.parametrize(
    "bad_allowlist", ["", "*", "example.org", "@example.org", "pilot@*.org"]
)
def test_iap_invalid_allowlist_fails_closed(iap_client, monkeypatch, bad_allowlist):
    monkeypatch.setenv("IAP_ALLOWED_EMAILS", bad_allowlist)
    response = iap_client.client.get(
        "/api/today", headers={ASSERTION_HEADER: _token(iap_client.private_key)}
    )
    assert response.status_code == 503
    assert response.json()["error"] == "gate_not_configured"


def test_iap_missing_audience_fails_closed_and_health_hides_configuration(
    iap_client, monkeypatch
):
    monkeypatch.delenv("IAP_AUDIENCE")
    health = iap_client.client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["auth_mode"] == "unavailable"
    assert health.json()["passcode"] is False
    assert health.json()["checks"]["auth"] == "not_configured"
    assert health.json()["ok"] is False
    assert AUDIENCE not in health.text
    assert EMAIL not in health.text

    protected = iap_client.client.get(
        "/api/today", headers={ASSERTION_HEADER: _token(iap_client.private_key)}
    )
    assert protected.status_code == 503
    assert protected.json()["error"] == "gate_not_configured"
    assert iap_client.fetches == []


def test_iap_unknown_mode_fails_closed_in_health_and_routes(iap_client, monkeypatch):
    monkeypatch.setenv("F42_AUTH_MODE", "anything_else")
    health = iap_client.client.get("/api/health")
    assert health.json()["auth_mode"] == "unavailable"
    assert health.json()["passcode"] is False
    assert health.json()["checks"]["auth"] == "not_configured"
    assert health.json()["ok"] is False
    response = iap_client.client.get(
        "/api/today", headers={ASSERTION_HEADER: _token(iap_client.private_key)}
    )
    assert response.status_code == 503
    assert response.json()["error"] == "gate_not_configured"


def test_default_passcode_mode_remains_active(iap_client, monkeypatch):
    monkeypatch.delenv("F42_AUTH_MODE")
    health = iap_client.client.get("/api/health")
    assert health.json()["auth_mode"] == "passcode"
    assert health.json()["passcode"] is True
    assert health.json()["checks"]["auth"] == "ok"
    response = iap_client.client.get(
        "/api/today", headers={"X-Passcode": PASSCODE}
    )
    assert response.status_code == 200
