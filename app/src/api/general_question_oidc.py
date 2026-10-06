"""Google-signed worker authentication for the fixed staging audience and invoker."""

import math
import re
import time
from types import SimpleNamespace

import requests
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import id_token

from src.api.deployment_contract import REQUIRED_SERVICE_ACCOUNT

_AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
_CERTIFICATES = "https://www.googleapis.com/oauth2/v1/certs"
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", re.I)


class WorkerOIDCError(ValueError):
    """A controlled authentication refusal containing no supplied token or claims."""

    def __init__(self, code, status_code):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _refuse(code="worker_oidc_invalid", status_code=401):
    raise WorkerOIDCError(code, status_code) from None


def verify_question_worker_authorization(authorization_headers, *, deadline):
    """Verify the sole Authorization header before entering the worker controller."""
    if (
        type(authorization_headers) is not list
        or len(authorization_headers) != 1
        or type(authorization_headers[0]) is not str
        or len(authorization_headers[0]) > 16384
    ):
        _refuse()
    match = _BEARER.fullmatch(authorization_headers[0])
    if match is None:
        _refuse()
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        _refuse("worker_oidc_unavailable", 503)

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            _refuse("worker_oidc_unavailable", 503)
        return min(5.0, seconds)

    remaining()
    fetched = False
    with requests.Session() as session:
        session.trust_env = False

        def certificate_request(url, method="GET", **kwargs):
            nonlocal fetched
            if fetched or url != _CERTIFICATES or method != "GET" or kwargs:
                _refuse("worker_oidc_unavailable", 503)
            fetched = True
            try:
                with session.get(
                    url,
                    timeout=remaining(),
                    allow_redirects=False,
                    stream=True,
                    verify=True,
                ) as response:
                    if response.status_code != 200:
                        _refuse("worker_oidc_unavailable", 503)
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=16384):
                        remaining()
                        size += len(chunk)
                        if size > 262144:
                            _refuse("worker_oidc_unavailable", 503)
                        chunks.append(chunk)
                    remaining()
                    return SimpleNamespace(status=200, data=b"".join(chunks))
            except requests.RequestException:
                _refuse("worker_oidc_unavailable", 503)

        try:
            claims = id_token.verify_oauth2_token(
                match[1],
                certificate_request,
                audience=_AUDIENCE,
                clock_skew_in_seconds=0,
            )
        except WorkerOIDCError:
            raise
        except (
            GoogleAuthError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            RecursionError,
        ):
            _refuse()
    remaining()
    now = time.time()
    if (
        type(claims) is not dict
        or claims.get("iss")
        not in ("accounts.google.com", "https://accounts.google.com")
        or claims.get("aud") != _AUDIENCE
        or type(claims.get("iat")) is not int
        or type(claims.get("exp")) is not int
        or not claims["iat"] <= now < claims["exp"]
        or claims["exp"] <= claims["iat"]
    ):
        _refuse()
    if (
        claims.get("email") != REQUIRED_SERVICE_ACCOUNT
        or claims.get("email_verified") is not True
    ):
        _refuse("worker_oidc_forbidden", 403)
    subject = claims.get("sub")
    if (
        type(subject) is not str
        or not subject
        or len(subject) > 255
        or not subject.isascii()
        or not subject.isprintable()
    ):
        _refuse()
    return {
        "email": REQUIRED_SERVICE_ACCOUNT,
        "subject": subject,
        "audience": _AUDIENCE,
        "expires_at": claims["exp"],
    }
