import base64
import copy
import importlib
import json
import logging
import socket
import time

import pytest
import requests
from google.auth.credentials import AnonymousCredentials
from google.auth.transport.requests import AuthorizedSession

from tests.unit.test_question_worker_result import EXPECTED


def module():
    return importlib.import_module("src.api.general_question_queue")


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "service_headers",
        "header_case",
        "wrong_service_header",
        "unknown_header",
        "duplicate_header",
        "extra_auth",
        "wrong_body",
        "wrong_name",
        "wrong_method",
        "wrong_url",
        "wrong_principal",
        "wrong_content_type",
        "conflict",
        "lost_ack",
        "missing",
        "mutated",
        "dispatch_deadline_mismatch",
        "redirect",
        "create_forbidden",
        "readback_forbidden",
        "readback_timeout",
        "invalid_json",
    ],
)
def test_actual_transport_keeps_exact_task_identity_and_never_reposts(
    monkeypatch, mode, caplog
):
    sent, stored = [], {}

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            sent.append((request.method, request.url, kwargs))
            assert request.url.startswith(
                "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/"
            )
            response = requests.Response()
            response.status_code = 200
            response.request, response.url = request, request.url
            if request.method == "POST":
                body = json.loads(request.body)
                stored.update(body["task"])
                assert stored["dispatchDeadline"] == "270s"
                assert body["responseView"] == "FULL"
                assert (
                    json.loads(base64.b64decode(stored["httpRequest"]["body"]))
                    == EXPECTED
                )
                if mode == "lost_ack":
                    raise requests.Timeout("provider-secret-sentinel")
                if mode == "conflict":
                    response.status_code = 409
                elif mode == "redirect":
                    response.status_code = 307
                    response.headers["Location"] = "https://evil.invalid/queue"
                elif mode == "create_forbidden":
                    response.status_code = 403
            elif mode == "missing":
                response.status_code = 404
            elif mode == "readback_forbidden":
                response.status_code = 403
            elif mode == "readback_timeout":
                raise requests.Timeout("provider-secret-sentinel")
            body = copy.deepcopy(stored)
            if request.method == "GET":
                headers = body["httpRequest"]["headers"]
                if mode in ("service_headers", "wrong_service_header"):
                    headers.update(
                        {
                            "Host": "reviewed---listening-post-staging-fibxg5ynpq-uc.a.run.app",
                            "Content-Length": str(
                                len(base64.b64decode(body["httpRequest"]["body"]))
                            ),
                            "User-Agent": "Google-Cloud-Tasks",
                        }
                    )
                    if mode == "wrong_service_header":
                        headers["Host"] = "evil.invalid"
                elif mode == "header_case":
                    body["httpRequest"]["headers"] = {
                        "content-type": "application/json"
                    }
                elif mode == "unknown_header":
                    headers["Authorization"] = "Bearer unexpected"
                elif mode == "duplicate_header":
                    headers["content-type"] = "application/json"
                elif mode == "extra_auth":
                    body["httpRequest"]["oauthToken"] = {
                        "serviceAccountEmail": "unexpected"
                    }
                elif mode == "wrong_body":
                    body["httpRequest"]["body"] = "e30="
                elif mode == "wrong_name":
                    body["name"] += "other"
                elif mode == "wrong_method":
                    body["httpRequest"]["httpMethod"] = "GET"
                elif mode == "wrong_url":
                    body["httpRequest"]["url"] = "https://evil.invalid"
                elif mode == "wrong_principal":
                    body["httpRequest"]["oidcToken"]["serviceAccountEmail"] = (
                        "unexpected"
                    )
                elif mode == "wrong_content_type":
                    headers["Content-Type"] = "text/plain"
            if request.method == "GET" and mode == "mutated":
                body["httpRequest"]["oidcToken"]["audience"] = "https://evil.invalid"
            if request.method == "GET" and mode == "dispatch_deadline_mismatch":
                body["dispatchDeadline"] = "210s"
            response._content = json.dumps(body).encode()
            if request.method == "GET" and mode == "invalid_json":
                response._content = b"provider-secret-sentinel"
            return response

        def close(self):
            pass

    original = AuthorizedSession.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.mount("https://", Adapter())

    monkeypatch.setattr(AuthorizedSession, "__init__", initialize)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *args: pytest.fail("network forbidden")
    )
    credentials = AnonymousCredentials()
    credentials.service_account_email = (
        "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    queue = module().GeneralQuestionQueue(
        credentials,
        worker_url="https://reviewed---listening-post-staging-fibxg5ynpq-uc.a.run.app/internal/general-question/execute",
        deployment_digest=EXPECTED["deployment_digest"],
    )
    result = queue.enqueue(EXPECTED, deadline=time.monotonic() + 10)
    assert result["state"] == (
        "verified"
        if mode in ("success", "service_headers", "header_case", "conflict", "lost_ack")
        else "ambiguous"
    )
    assert sum(method == "POST" for method, _, _ in sent) == 1
    assert all("evil.invalid" not in url for _, url, _ in sent)
    assert result["task_name"].endswith(
        "/tasks/question_00000000000000000000000000000001"
    )
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"code":')
    ]
    expected = {
        "success": "queue_readback_verified",
        "service_headers": "queue_readback_verified",
        "header_case": "queue_readback_verified",
        "wrong_service_header": "queue_readback_mismatch",
        "unknown_header": "queue_readback_mismatch",
        "duplicate_header": "queue_readback_mismatch",
        "extra_auth": "queue_readback_mismatch",
        "wrong_body": "queue_readback_mismatch",
        "wrong_name": "queue_readback_mismatch",
        "wrong_method": "queue_readback_mismatch",
        "wrong_url": "queue_readback_mismatch",
        "wrong_principal": "queue_readback_mismatch",
        "wrong_content_type": "queue_readback_mismatch",
        "conflict": "queue_create_conflict",
        "lost_ack": "queue_create_timeout",
        "missing": "queue_readback_missing",
        "mutated": "queue_readback_mismatch",
        "dispatch_deadline_mismatch": "queue_readback_mismatch",
        "redirect": "queue_create_redirect_refused",
        "create_forbidden": "queue_create_http_rejected",
        "readback_forbidden": "queue_readback_http_rejected",
        "readback_timeout": "queue_readback_timeout",
        "invalid_json": "queue_readback_invalid",
    }[mode]
    assert expected in [row["code"] for row in records]
    assert all(row["request_id"] == EXPECTED["request_id"] for row in records)
    assert all(
        set(row)
        == {
            "contract_version",
            "request_id",
            "operation",
            "state",
            "code",
            "http_status",
        }
        for row in records
    )
    assert "provider-secret" not in caplog.text


_WORKER_HOST = "reviewed---listening-post-staging-fibxg5ynpq-uc.a.run.app"
_DETAIL_CONTRACT = "general_question_queue_readback_detail_v1"


def _service_headers(task):
    return {
        "Host": _WORKER_HOST,
        "Content-Length": str(len(base64.b64decode(task["httpRequest"]["body"]))),
        "User-Agent": "Google-Cloud-Tasks",
    }


def _enqueue_with_readback(monkeypatch, caplog, mutate):
    """Create once, then read back the stored task after mutate(task) edits it."""
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    stored = {}

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.request, response.url = request, request.url
            if request.method == "POST":
                stored.update(json.loads(request.body)["task"])
                body = copy.deepcopy(stored)
            else:
                body = copy.deepcopy(stored)
                mutate(body)
            response._content = json.dumps(body).encode()
            return response

        def close(self):
            pass

    original = AuthorizedSession.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.mount("https://", Adapter())

    monkeypatch.setattr(AuthorizedSession, "__init__", initialize)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *args: pytest.fail("network forbidden")
    )
    credentials = AnonymousCredentials()
    credentials.service_account_email = (
        "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    queue = module().GeneralQuestionQueue(
        credentials,
        worker_url="https://" + _WORKER_HOST + "/internal/general-question/execute",
        deployment_digest=EXPECTED["deployment_digest"],
    )
    result = queue.enqueue(EXPECTED, deadline=time.monotonic() + 10)
    rows = [json.loads(row.getMessage()) for row in caplog.records]
    codes = [row.get("code") for row in rows]
    details = [row for row in rows if row.get("contract_version") == _DETAIL_CONTRACT]
    return result, codes, details


@pytest.mark.parametrize(
    "present",
    [
        ("Host",),
        ("Content-Length",),
        ("User-Agent",),
        ("Host", "User-Agent"),
        ("Content-Length", "User-Agent"),
        ("Host", "Content-Length"),
    ],
)
def test_readback_tolerates_any_subset_of_the_cloud_tasks_service_headers(
    monkeypatch, caplog, present
):
    def mutate(task):
        added = _service_headers(task)
        task["httpRequest"]["headers"].update({key: added[key] for key in present})

    result, codes, details = _enqueue_with_readback(monkeypatch, caplog, mutate)
    assert result["state"] == "verified"
    assert "queue_readback_verified" in codes
    assert "queue_readback_mismatch" not in codes
    assert details == []


def _add_headers(extra, service=True):
    def mutate(task):
        headers = task["httpRequest"]["headers"]
        if service:
            headers.update(_service_headers(task))
        headers.update(extra)

    return mutate


def _set(path, value):
    def mutate(task):
        target = task
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(path):
    def mutate(task):
        target = task
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _chain(*steps):
    def mutate(task):
        for step in steps:
            step(task)

    return mutate


_SECRET = "provider-secret-sentinel"


@pytest.mark.parametrize(
    ("mutate", "mismatch_codes", "header_names", "header_count"),
    [
        pytest.param(
            _add_headers({"X-CloudTasks-TaskName": "question_1"}),
            ["readback_header_names"],
            ["x-cloudtasks-taskname"],
            1,
            id="cloud_tasks_task_name_header",
        ),
        pytest.param(
            _add_headers(
                {
                    "X-CloudTasks-QueueName": "oi-general-question-staging",
                    "X-CloudTasks-TaskRetryCount": "0",
                },
                service=False,
            ),
            ["readback_header_names"],
            ["x-cloudtasks-queuename", "x-cloudtasks-taskretrycount"],
            2,
            id="cloud_tasks_queue_headers_without_service_headers",
        ),
        pytest.param(
            _add_headers({"Authorization": "Bearer " + _SECRET}),
            ["readback_header_names"],
            ["authorization"],
            1,
            id="unknown_header_value_never_logged",
        ),
        pytest.param(
            _add_headers({"Host": "evil.invalid"}),
            ["readback_header_values"],
            [],
            0,
            id="wrong_host_value",
        ),
        pytest.param(
            _add_headers({"User-Agent": _SECRET}, service=False),
            ["readback_header_values"],
            [],
            0,
            id="wrong_user_agent_value",
        ),
        pytest.param(
            _add_headers({"Content-Length": "1"}),
            ["readback_header_values"],
            [],
            0,
            id="wrong_content_length_value",
        ),
        pytest.param(
            _add_headers({"Content-Type": "text/" + _SECRET}),
            ["readback_header_values"],
            [],
            0,
            id="wrong_content_type_value",
        ),
        pytest.param(
            _add_headers({"content-type": "application/json"}, service=False),
            ["readback_header_names"],
            [],
            0,
            id="duplicate_header_case",
        ),
        pytest.param(
            _set(("httpRequest", "headers"), {"User-Agent": "Google-Cloud-Tasks"}),
            ["readback_header_names"],
            [],
            0,
            id="missing_content_type",
        ),
        pytest.param(
            _set(("httpRequest", "headers"), "application/json"),
            ["readback_header_names"],
            [],
            0,
            id="headers_not_an_object",
        ),
        pytest.param(
            _drop(("httpRequest", "headers")),
            ["readback_header_names"],
            [],
            0,
            id="headers_absent",
        ),
        pytest.param(
            _add_headers(
                {
                    "X-Evil\r\nInjected": "1",
                    "x-" + "a" * 60: "1",
                    "X_Underscore": "1",
                    "X-Ok": "1",
                    _SECRET + " " + _SECRET: "1",
                }
            ),
            ["readback_header_names"],
            ["x-ok"],
            5,
            id="hostile_header_names_filtered_and_counted",
        ),
        pytest.param(
            _add_headers({"X-Extra-%02d" % index: "1" for index in range(20)}),
            ["readback_header_names"],
            ["x-extra-%02d" % index for index in range(16)],
            20,
            id="header_names_capped",
        ),
        pytest.param(
            _set(("name",), "projects/other/tasks/" + _SECRET),
            ["readback_name"],
            [],
            0,
            id="name",
        ),
        pytest.param(
            _set(("dispatchDeadline",), "210s"),
            ["readback_deadline"],
            [],
            0,
            id="deadline",
        ),
        pytest.param(
            _drop(("dispatchDeadline",)),
            ["readback_deadline"],
            [],
            0,
            id="deadline_absent",
        ),
        pytest.param(
            _set(("httpRequest", "url"), "https://evil.invalid/" + _SECRET),
            ["readback_url"],
            [],
            0,
            id="url",
        ),
        pytest.param(
            _set(("httpRequest", "body"), "e30="),
            ["readback_body"],
            [],
            0,
            id="body",
        ),
        pytest.param(
            _set(("httpRequest", "httpMethod"), "GET"),
            ["readback_method"],
            [],
            0,
            id="method",
        ),
        pytest.param(
            _set(("httpRequest", "oidcToken", "audience"), "https://evil.invalid"),
            ["readback_oidc"],
            [],
            0,
            id="oidc_audience",
        ),
        pytest.param(
            _drop(("httpRequest", "oidcToken")),
            ["readback_oidc"],
            [],
            0,
            id="oidc_absent",
        ),
        pytest.param(
            _set(("httpRequest", "oauthToken"), {"serviceAccountEmail": _SECRET}),
            ["readback_extra_keys"],
            [],
            0,
            id="extra_request_key",
        ),
        pytest.param(
            _set(("httpRequest",), "POST " + _SECRET),
            ["readback_http_request"],
            [],
            0,
            id="request_not_an_object",
        ),
        pytest.param(
            _chain(
                _add_headers({"X-CloudTasks-TaskName": "question_1"}),
                _set(("httpRequest", "body"), "e30="),
                _set(("dispatchDeadline",), "210s"),
            ),
            ["readback_body", "readback_deadline", "readback_header_names"],
            ["x-cloudtasks-taskname"],
            1,
            id="several_fields",
        ),
    ],
)
def test_readback_mismatch_names_each_differing_field_without_values(
    monkeypatch, caplog, mutate, mismatch_codes, header_names, header_count
):
    result, codes, details = _enqueue_with_readback(monkeypatch, caplog, mutate)
    assert result["state"] == "ambiguous"
    assert "queue_readback_mismatch" in codes
    assert details == [
        {
            "contract_version": _DETAIL_CONTRACT,
            "request_id": EXPECTED["request_id"],
            "operation": "queue_readback",
            "mismatch_codes": mismatch_codes,
            "unexpected_header_names": header_names,
            "unexpected_header_count": header_count,
        }
    ]
    assert _SECRET not in caplog.text
    assert "evil.invalid" not in caplog.text
    assert "oauthToken" not in caplog.text
    assert "Injected" not in caplog.text


def test_readback_detail_emitter_accepts_only_the_closed_vocabulary(caplog):
    process = module()
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    process.emit_readback_mismatch_detail(
        ["readback_body", "queue_readback_mismatch", _SECRET, None, "readback_body"],
        ["authorization", "Authorization", _SECRET, 7, "x-" + "b" * 60],
        request_id=_SECRET,
    )
    process.emit_readback_mismatch_detail(
        ["readback_url"], ["authorization"], request_id=EXPECTED["request_id"]
    )
    rows = [json.loads(row.getMessage()) for row in caplog.records]
    assert rows == [
        {
            "contract_version": _DETAIL_CONTRACT,
            "request_id": None,
            "operation": "queue_readback",
            "mismatch_codes": ["readback_body"],
            "unexpected_header_names": [],
            "unexpected_header_count": 0,
        },
        {
            "contract_version": _DETAIL_CONTRACT,
            "request_id": EXPECTED["request_id"],
            "operation": "queue_readback",
            "mismatch_codes": ["readback_url"],
            "unexpected_header_names": [],
            "unexpected_header_count": 0,
        },
    ]
    assert _SECRET not in caplog.text
