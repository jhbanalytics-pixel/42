import base64
import importlib
import json
import os
import platform
import sys
import time
from importlib.metadata import version
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from starlette.requests import Request

project_root = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "core" / "api" / "auth.py").is_file()
)
sys.path.insert(0, str(project_root))

auth = importlib.import_module("core.api.auth")

AUDIENCE = "/projects/123456789/locations/us-central1/services/f42-api"
EMAIL = "pilot@example.org"
KID = "in-memory-probe-key"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def token(private_key, claims=None):
    now = int(time.time())
    header = {"alg": "ES256", "kid": KID, "typ": "JWT"}
    payload = {
        "aud": AUDIENCE,
        "email": EMAIL,
        "exp": now + 300,
        "iat": now - 30,
        "iss": "https://cloud.google.com/iap",
    }
    payload.update(claims or {})
    signing_input = (
        f"{b64(json.dumps(header, separators=(',', ':')).encode())}."
        f"{b64(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    der = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{b64(signature)}"


def request(method, headers):
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
        "headers": [(k.lower().encode("ascii"), v.encode("ascii")) for k, v in headers.items()],
        "server": ("probe", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }
    return Request(scope)


def expect_allowed(name, method, assertion):
    try:
        auth.require_passcode(
            request(method, {"x-goog-iap-jwt-assertion": assertion})
        )
    except Exception:
        raise AssertionError(name) from None
    print(f"PASS {name}")


def expect_denied(name, method, headers, status, code):
    try:
        auth.require_passcode(request(method, headers))
    except auth.ApiError as exc:
        if exc.status != status or exc.error != code:
            raise AssertionError(name) from None
    except Exception:
        raise AssertionError(name) from None
    else:
        raise AssertionError(name)
    print(f"PASS {name}")


def main():
    print(f"python={platform.python_version()}")
    print(f"cryptography={version('cryptography')}")
    print(f"google-auth={version('google-auth')}")
    print(f"requests={version('requests')}")

    saved = {
        name: os.environ.get(name)
        for name in ("F42_AUTH_MODE", "IAP_AUDIENCE", "IAP_ALLOWED_EMAILS", "UI_PASSCODE")
    }
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    certificates = {KID: public_pem}
    for index in range(4):
        rotated_key = ec.generate_private_key(ec.SECP256R1())
        certificates[f"rotated-key-{index}"] = rotated_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    class CertResponse:
        status = 200
        data = json.dumps(certificates).encode("utf-8")

    class MockRequest:
        def __call__(self, url, method="GET", timeout=None, **kwargs):
            if (
                url != auth.IAP_CERTS_URL
                or method != "GET"
                or timeout != auth.IAP_CERT_TIMEOUT
            ):
                raise RuntimeError
            return CertResponse()

    original_request = auth.GoogleAuthRequest
    auth.GoogleAuthRequest = lambda: MockRequest()
    try:
        os.environ["F42_AUTH_MODE"] = "iap_readonly"
        os.environ["IAP_AUDIENCE"] = AUDIENCE
        os.environ["IAP_ALLOWED_EMAILS"] = EMAIL
        os.environ["UI_PASSCODE"] = "synthetic-passcode"

        approved = token(private_key)
        expect_allowed("approved_get", "GET", approved)

        forged = approved.split(".")
        forged[2] = b64(b"\x01" * 64)
        expect_denied(
            "invalid_signature",
            "GET",
            {"x-goog-iap-jwt-assertion": ".".join(forged)},
            401,
            "unauthorized",
        )
        expect_denied(
            "wrong_audience",
            "GET",
            {"x-goog-iap-jwt-assertion": token(private_key, {"aud": "/wrong"})},
            401,
            "unauthorized",
        )
        expect_denied(
            "expired",
            "GET",
            {"x-goog-iap-jwt-assertion": token(private_key, {"exp": int(time.time()) - 60})},
            401,
            "unauthorized",
        )
        expect_denied(
            "unlisted_identity",
            "GET",
            {"x-goog-iap-jwt-assertion": token(private_key, {"email": "other@example.org"})},
            401,
            "unauthorized",
        )

        os.environ.pop("IAP_AUDIENCE")
        expect_denied(
            "missing_configuration",
            "GET",
            {"x-goog-iap-jwt-assertion": approved},
            503,
            "gate_not_configured",
        )
        os.environ["IAP_AUDIENCE"] = AUDIENCE

        expect_denied(
            "passcode_fallback_rejected",
            "GET",
            {
                "x-passcode": "synthetic-passcode",
                "x-goog-authenticated-user-email": EMAIL,
            },
            401,
            "unauthorized",
        )
        expect_denied(
            "protected_post_guard",
            "POST",
            {"x-goog-iap-jwt-assertion": approved},
            403,
            "read_only_pilot",
        )
    finally:
        auth.GoogleAuthRequest = original_request
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL probe")
        raise SystemExit(1)
