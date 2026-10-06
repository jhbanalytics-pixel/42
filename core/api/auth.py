"""Authentication gates and per-IP limits for f42-api (contract.md section 2).

Lifted from app/src/api/main.py: require_passcode, _client_ip and the auth and
ask sliding-window limiters. The LP_ALLOW_OPEN_GATE escape is not lifted, so an
unset UI_PASSCODE always fails closed. Counters live in memory per instance.
"""

import base64
import binascii
import hmac
import json
import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Request
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

logger = logging.getLogger("f42.api.auth")

SAST = timezone(timedelta(hours=2))
DEFAULT_ASK_PER_IP_DAILY = 100
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERT_TIMEOUT = 5
IAP_CLOCK_SKEW_SECONDS = 30
IAP_MAX_TOKEN_LIFETIME_SECONDS = 660
AUTH_UNAVAILABLE_MESSAGE = "Authentication is unavailable. Try again shortly."
GATE_NOT_CONFIGURED_MESSAGE = "The authentication gate is not configured."


class ApiError(Exception):
    """An error the app returns as {"error": code, "message": text}."""

    def __init__(self, status: int, error: str, message: str):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def client_ip(request: Request) -> str:
    # Cloud Run's front end appends the real caller IP as the RIGHTMOST
    # X-Forwarded-For hop. Earlier hops are client supplied and spoofable, so
    # the limiters key on the rightmost entry.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _expected_passcode() -> str:
    expected = os.environ.get("UI_PASSCODE", "")
    if not expected:
        raise ApiError(503, "gate_not_configured", "The passcode gate is not configured.")
    return expected


def passcode_matches(supplied: str) -> bool:
    expected = _expected_passcode()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def configured_auth_mode() -> str:
    return os.environ.get("F42_AUTH_MODE", "passcode")


def health_status() -> dict[str, Any]:
    mode = configured_auth_mode()
    if mode == "passcode" and os.environ.get("UI_PASSCODE", ""):
        return {"auth_mode": "passcode", "passcode": True, "check": "ok"}
    if mode == "iap_readonly":
        try:
            _iap_configuration()
        except ApiError:
            pass
        else:
            return {"auth_mode": "iap_readonly", "passcode": False, "check": "ok"}
    return {"auth_mode": "unavailable", "passcode": False, "check": "not_configured"}


def _iap_configuration() -> tuple[str, frozenset[str]]:
    audience = os.environ.get("IAP_AUDIENCE", "")
    raw_allowlist = os.environ.get("IAP_ALLOWED_EMAILS", "")
    if not audience or audience != audience.strip() or any(c.isspace() for c in audience):
        raise ApiError(503, "gate_not_configured", GATE_NOT_CONFIGURED_MESSAGE)

    emails = [entry.strip() for entry in raw_allowlist.split(",")]
    if not emails or any(
        not email
        or "*" in email
        or any(c.isspace() for c in email)
        or email.count("@") != 1
        or not email.split("@", 1)[0]
        or not email.split("@", 1)[1]
        for email in emails
    ):
        raise ApiError(503, "gate_not_configured", GATE_NOT_CONFIGURED_MESSAGE)
    return audience, frozenset(emails)


def _unauthorized_iap() -> None:
    raise ApiError(401, "unauthorized", "Missing or invalid authentication.")


def _decode_jwt_segment(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _iap_cert_request(url, method="GET", body=None, headers=None, **kwargs):
    kwargs.pop("timeout", None)
    response = GoogleAuthRequest()(
        url,
        method=method,
        body=body,
        headers=headers,
        timeout=IAP_CERT_TIMEOUT,
        **kwargs,
    )
    _validate_iap_cert_response(response)
    return response


def _validate_iap_cert_response(response) -> None:
    try:
        if response.status != 200:
            raise ValueError
        certificates = json.loads(response.data.decode("utf-8"))
        if not isinstance(certificates, dict) or not certificates:
            raise ValueError
        for kid, pem in certificates.items():
            if not isinstance(kid, str) or not kid or not isinstance(pem, str) or not pem:
                raise ValueError
            encoded = pem.encode("ascii")
            if b"-----BEGIN CERTIFICATE-----" in encoded:
                key = x509.load_pem_x509_certificate(encoded).public_key()
            else:
                key = serialization.load_pem_public_key(encoded)
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
                key.curve, ec.SECP256R1
            ):
                raise ValueError
    except Exception:
        raise ApiError(503, "auth_unavailable", AUTH_UNAVAILABLE_MESSAGE) from None


def _timestamp(claims: dict[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _unauthorized_iap()
    try:
        result = float(value)
    except OverflowError:
        _unauthorized_iap()
    if not math.isfinite(result):
        _unauthorized_iap()
    return result


def _verify_iap_token(token: str, audience: str, allowed_emails: frozenset[str]) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        _unauthorized_iap()
    try:
        header = json.loads(_decode_jwt_segment(parts[0]))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        _unauthorized_iap()
    if (
        not isinstance(header, dict)
        or header.get("alg") != "ES256"
        or not isinstance(header.get("kid"), str)
        or not header["kid"]
    ):
        _unauthorized_iap()

    try:
        claims = id_token.verify_token(
            token,
            request=_iap_cert_request,
            audience=audience,
            certs_url=IAP_CERTS_URL,
            clock_skew_in_seconds=IAP_CLOCK_SKEW_SECONDS,
        )
    except ApiError:
        raise
    except google_auth_exceptions.TransportError:
        raise ApiError(503, "auth_unavailable", AUTH_UNAVAILABLE_MESSAGE) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(503, "auth_unavailable", AUTH_UNAVAILABLE_MESSAGE) from None
    except Exception:
        _unauthorized_iap()

    if not isinstance(claims, dict):
        _unauthorized_iap()
    issued_at = _timestamp(claims, "iat")
    expires_at = _timestamp(claims, "exp")
    now = time.time()
    if (
        expires_at < issued_at
        or expires_at - issued_at > IAP_MAX_TOKEN_LIFETIME_SECONDS
    ):
        _unauthorized_iap()
    if "nbf" in claims and _timestamp(claims, "nbf") > now + IAP_CLOCK_SKEW_SECONDS:
        _unauthorized_iap()
    if (
        claims.get("aud") != audience
        or claims.get("iss") != IAP_ISSUER
    ):
        _unauthorized_iap()
    if not isinstance(claims.get("email"), str) or claims["email"] not in allowed_emails:
        _unauthorized_iap()


def verify_iap_assertion(request: Request) -> None:
    audience, allowed_emails = _iap_configuration()
    assertion = request.headers.get("x-goog-iap-jwt-assertion", "")
    if not assertion:
        _unauthorized_iap()
    _verify_iap_token(assertion, audience, allowed_emails)


def require_passcode(request: Request) -> None:
    """FastAPI dependency for the configured passcode or IAP boundary.

    In passcode mode, failures share auth_limiter with /api/auth/verify. An IP
    over that limit gets 429 whatever it sends until the window clears.
    """
    mode = configured_auth_mode()
    if mode == "iap_readonly":
        verify_iap_assertion(request)
        if request.method.upper() not in {"GET", "HEAD"}:
            raise ApiError(403, "read_only_pilot", "This pilot is read only.")
        return
    if mode != "passcode":
        raise ApiError(503, "gate_not_configured", GATE_NOT_CONFIGURED_MESSAGE)

    supplied = request.headers.get("x-passcode", "")
    if not supplied:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            supplied = token.strip()
    auth_limiter.check(request, count=False)
    if not passcode_matches(supplied):
        auth_limiter.check(request)
        raise ApiError(401, "unauthorized", "Missing or wrong passcode.")


class SlidingWindow:
    """At most `limit` hits per `window` seconds per client IP."""

    def __init__(self, limit: int, window: float, message: str):
        self.limit = limit
        self.window = window
        self.message = message
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def check(self, request: Request, count: bool = True) -> None:
        """429 when the IP is at the limit; otherwise record a hit if `count`."""
        now = time.monotonic()
        ip = client_ip(request)
        with self.lock:
            for key in list(self.hits):
                hits = self.hits[key]
                while hits and now - hits[0] > self.window:
                    hits.popleft()
                if not hits:
                    del self.hits[key]
            if len(self.hits.get(ip, ())) >= self.limit:
                logger.warning("rate limit tripped by client %s", ip)
                raise ApiError(429, "rate_limited", self.message)
            if count:
                self.hits.setdefault(ip, deque()).append(now)


auth_limiter = SlidingWindow(
    10, 60.0, "Too many passcode attempts. Wait a minute and try again."
)
ask_limiter = SlidingWindow(
    30, 10.0, "Rate limit reached: 30 requests per 10 seconds. Try again shortly."
)


def sast_date() -> str:
    return datetime.now(SAST).date().isoformat()


def ask_per_ip_daily() -> int:
    try:
        return int(os.environ.get("ASK_PER_IP_DAILY", DEFAULT_ASK_PER_IP_DAILY))
    except ValueError:
        return DEFAULT_ASK_PER_IP_DAILY


class DailyQuestions:
    """Live questions per client IP per SAST day. Replay asks never reach it."""

    def __init__(self):
        self.counts: dict[str, tuple[str, int]] = {}
        self.lock = threading.Lock()

    def reserve(self, ip: str) -> None:
        today = sast_date()
        limit = ask_per_ip_daily()
        with self.lock:
            for key in [k for k, (day, _) in self.counts.items() if day != today]:
                del self.counts[key]
            _, used = self.counts.get(ip, (today, 0))
            if used >= limit:
                raise ApiError(
                    429,
                    "daily_question_limit",
                    f"The daily limit of {limit} live questions from this network is used up. "
                    "Replay questions still work, and the count resets at midnight SAST.",
                )
            self.counts[ip] = (today, used + 1)

    def release(self, ip: str) -> None:
        """Give back a reserved question the agent did not accept."""
        today = sast_date()
        with self.lock:
            day, used = self.counts.get(ip, (today, 0))
            if day == today and used > 0:
                self.counts[ip] = (day, used - 1)


daily_questions = DailyQuestions()
