"""One fixed Cloud Tasks enqueue and exact same-name reconciliation."""

import base64
import json
import logging
import math
import re
import time
from urllib.parse import urlsplit

import requests
from google.auth.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

from src.api.deployment_contract import REQUIRED_SERVICE_ACCOUNT
from src.api.question_worker_process import _DIAGNOSTIC_LOGGER, emit_parent_diagnostic
from src.api.question_worker_protocol import (
    _digest,
    _read_reply,
    _uuid,
    encode_worker_input,
)

_QUEUE = (
    "projects/ogilvy-trends-v2/locations/us-central1/queues/oi-general-question-staging"
)
_BASE = "https://cloudtasks.googleapis.com/v2/"
_AUDIENCE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
_DISPATCH_DEADLINE = "270s"
_TARGET = re.compile(
    r"https://[a-z][a-z0-9-]*---listening-post-staging-fibxg5ynpq-uc\.a\.run\.app/internal/general-question/execute\Z"
)


# Closed per-field vocabulary for a Cloud Tasks readback that differs from the
# task this process created. Each code names a field, never its content.
_READBACK_DETAIL_CODES = frozenset(
    {
        "readback_name",
        "readback_deadline",
        "readback_http_request",
        "readback_method",
        "readback_url",
        "readback_body",
        "readback_oidc",
        "readback_header_names",
        "readback_header_values",
        "readback_extra_keys",
        "readback_unclassified",
    }
)
# Header names are logged only for a header name mismatch, only when they are a
# short lowercase token, and never with their values.
_READBACK_HEADER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
_READBACK_HEADER_NAME_LIMIT = 16


def emit_readback_mismatch_detail(codes, header_names=(), *, request_id=None):
    """Emit which readback fields differed, from a closed vocabulary only."""
    try:
        mismatch_codes = sorted(
            {
                code
                for code in codes
                if type(code) is str and code in _READBACK_DETAIL_CODES
            }
        )
        names = (
            {name for name in header_names if type(name) is str}
            if "readback_header_names" in mismatch_codes
            else set()
        )
        shown = sorted(
            name for name in names if _READBACK_HEADER_NAME.fullmatch(name) is not None
        )[:_READBACK_HEADER_NAME_LIMIT]
    except Exception:
        return
    if type(request_id) is not str or len(request_id) != 36 or not _uuid(request_id):
        request_id = None
    record = {
        "contract_version": "general_question_queue_readback_detail_v1",
        "request_id": request_id,
        "operation": "queue_readback",
        "mismatch_codes": mismatch_codes,
        "unexpected_header_names": shown,
        "unexpected_header_count": len(names),
    }
    try:
        _DIAGNOSTIC_LOGGER.log(
            logging.WARNING, json.dumps(record, sort_keys=True, separators=(",", ":"))
        )
    except Exception:
        pass


def _service_headers(worker_url, body):
    """Headers Cloud Tasks adds to a stored HTTP task, tolerated on readback.

    The FULL task view reports the request as Cloud Tasks will send it, so it
    may add Host (the target URL authority), Content-Length (the decoded body
    size) and User-Agent (the fixed value Google-Cloud-Tasks) to the one
    Content-Type header this module sets. Any subset of these is accepted, but
    only with exactly these values. Nothing else is tolerated: dispatch time
    headers such as X-CloudTasks-TaskName, or any other added header, make the
    readback a mismatch.
    """
    return {
        "host": urlsplit(worker_url).netloc,
        "content-length": str(len(body)),
        "user-agent": "Google-Cloud-Tasks",
    }


def _mismatch_detail(task, actual, request, service_headers):
    """Name the differing readback fields and unexpected header names only.

    This explains a mismatch the exact comparison already decided; it never
    changes that decision and never returns a value from the readback.
    """
    codes = set()
    header_names = []
    if actual.get("name") != task["name"]:
        codes.add("readback_name")
    if actual.get("dispatchDeadline") != task["dispatchDeadline"]:
        codes.add("readback_deadline")
    expected = task["httpRequest"]
    current = actual.get("httpRequest")
    if current != expected:
        if type(current) is not dict:
            codes.add("readback_http_request")
        else:
            for key, code in (
                ("httpMethod", "readback_method"),
                ("url", "readback_url"),
                ("body", "readback_body"),
                ("oidcToken", "readback_oidc"),
            ):
                if current.get(key) != expected[key]:
                    codes.add(code)
            if set(current) - set(expected):
                codes.add("readback_extra_keys")
            if current.get("headers") != expected["headers"]:
                headers = request.get("headers") if type(request) is dict else None
                if type(headers) is not dict:
                    codes.add("readback_header_names")
                else:
                    wanted = {
                        key.lower(): value for key, value in expected["headers"].items()
                    }
                    allowed = {**service_headers, **wanted}
                    lowered = [str(key).lower() for key in headers]
                    header_names = sorted(set(lowered) - set(allowed))
                    if (
                        header_names
                        or len(set(lowered)) != len(lowered)
                        or set(wanted) - set(lowered)
                    ):
                        codes.add("readback_header_names")
                    if any(
                        str(key).lower() in allowed
                        and value != allowed[str(key).lower()]
                        for key, value in headers.items()
                    ):
                        codes.add("readback_header_values")
    return sorted(codes or {"readback_unclassified"}), header_names


class GeneralQuestionQueue:
    def __init__(self, credentials, *, worker_url, deployment_digest):
        if (
            not isinstance(credentials, Credentials)
            or getattr(credentials, "service_account_email", None)
            != REQUIRED_SERVICE_ACCOUNT
            or type(worker_url) is not str
            or _TARGET.fullmatch(worker_url) is None
            or not _digest(deployment_digest)
        ):
            raise ValueError("question_queue_configuration_invalid")
        self.credentials = credentials
        self.worker_url = worker_url
        self.deployment_digest = deployment_digest

    def enqueue(self, invocation, *, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("question_queue_deadline")
        body = encode_worker_input("execute", invocation)
        if invocation["deployment_digest"] != self.deployment_digest:
            raise ValueError("question_queue_binding_invalid")
        name = _QUEUE + "/tasks/question_" + invocation["request_id"].replace("-", "")
        task = {
            "name": name,
            "dispatchDeadline": _DISPATCH_DEADLINE,
            "httpRequest": {
                "httpMethod": "POST",
                "url": self.worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(body).decode("ascii"),
                "oidcToken": {
                    "serviceAccountEmail": REQUIRED_SERVICE_ACCOUNT,
                    "audience": _AUDIENCE,
                },
            },
        }
        result = {"state": "ambiguous", "task_name": name}

        def emit(operation, state, code, status=None):
            emit_parent_diagnostic(
                operation,
                state,
                code,
                request_id=invocation["request_id"],
                http_status=status,
            )

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ValueError("question_queue_deadline")
            return min(5.0, seconds)

        with AuthorizedSession(self.credentials, max_refresh_attempts=0) as session:
            session.trust_env = False
            emit("queue_create", "started", "operation_started")
            try:
                response = session.post(
                    _BASE + _QUEUE + "/tasks",
                    json={"task": task, "responseView": "FULL"},
                    timeout=remaining(),
                    allow_redirects=False,
                )
                if response.status_code == 200:
                    emit("queue_create", "succeeded", "queue_create_acknowledged", 200)
                elif response.status_code == 409:
                    emit("queue_create", "reused", "queue_create_conflict", 409)
                elif 300 <= response.status_code <= 399:
                    emit(
                        "queue_create",
                        "unresolved",
                        "queue_create_redirect_refused",
                        response.status_code,
                    )
                else:
                    emit(
                        "queue_create",
                        "unresolved",
                        "queue_create_http_rejected",
                        response.status_code,
                    )
                if response.status_code not in (200, 409, 500, 502, 503, 504):
                    return result
            except requests.Timeout:
                emit("queue_create", "unresolved", "queue_create_timeout")
            except requests.RequestException:
                emit("queue_create", "unresolved", "queue_create_transport_unavailable")
            except ValueError:
                emit("queue_create", "unresolved", "queue_deadline_reached")
                raise
            emit("queue_readback", "started", "operation_started")
            try:
                response = session.get(
                    _BASE + name,
                    params={"responseView": "FULL"},
                    timeout=remaining(),
                    allow_redirects=False,
                )
                if response.status_code != 200:
                    code = (
                        "queue_readback_missing"
                        if response.status_code == 404
                        else "queue_readback_redirect_refused"
                        if 300 <= response.status_code <= 399
                        else "queue_readback_http_rejected"
                    )
                    emit("queue_readback", "unresolved", code, response.status_code)
                    return result
                if len(response.content) > 100000:
                    emit("queue_readback", "unresolved", "queue_readback_invalid", 200)
                    return result
                actual = _read_reply(response.content, max_bytes=100000)
                remaining()
            except requests.Timeout:
                emit("queue_readback", "unresolved", "queue_readback_timeout")
                return result
            except requests.RequestException:
                emit(
                    "queue_readback",
                    "unresolved",
                    "queue_readback_transport_unavailable",
                )
                return result
            except ValueError as error:
                code = (
                    "queue_deadline_reached"
                    if error.args == ("question_queue_deadline",)
                    else "queue_readback_invalid"
                )
                emit("queue_readback", "unresolved", code)
                return result
            request = actual.get("httpRequest")
            headers = request.get("headers") if type(request) is dict else None
            if type(headers) is dict:
                normalized = {key.lower(): value for key, value in headers.items()}
                service_headers = _service_headers(self.worker_url, body)
                if len(normalized) == len(headers) and all(
                    normalized.get(key, value) == value
                    for key, value in service_headers.items()
                ):
                    for key in service_headers:
                        normalized.pop(key, None)
                    if normalized == {"content-type": "application/json"}:
                        actual["httpRequest"] = {
                            **request,
                            "headers": task["httpRequest"]["headers"],
                        }
            if all(actual.get(key) == value for key, value in task.items()):
                result["state"] = "verified"
                emit("queue_readback", "succeeded", "queue_readback_verified", 200)
            else:
                emit("queue_readback", "unresolved", "queue_readback_mismatch", 200)
                codes, header_names = _mismatch_detail(
                    task, actual, request, _service_headers(self.worker_url, body)
                )
                emit_readback_mismatch_detail(
                    codes, header_names, request_id=invocation["request_id"]
                )
        return result
