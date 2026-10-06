"""Native adapter over a fake transport: request paths, bodies, response mapping,
the generation precondition, the token source, and release.py plan, apply and
verify end to end against the native adapter for the tree's activation shape.
"""

import base64
import copy
import inspect
import json
import subprocess
import threading
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ops.deploy import release
from ops.deploy import release_native_adapter as native
from ops.tests.test_foundation_release import (
    MANIFEST_PATH,
    NEW_IMAGE,
    PARENT,
    QUEUE_API,
    RID,
    SERVICE,
    SERVICE_API,
    Scenario,
    authority,
    canonical,
    fake,
    make_binding,
    make_policy,
    release_index_fixture,
    run_cli,
    terminal_requests,
    write_json,
)

NATIVE_PATH = Path(release.__file__).resolve().parent / "release_native_adapter.py"
TOKEN = "fake-token"
LEDGER = release.LEDGER_OBJECT
OPERATION = SERVICE_API.rsplit("/services/", 1)[0] + "/operations/op-1"


def _json(status, value):
    return {
        "status": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(value).encode("utf-8"),
    }


class FakeTransport:
    """Canned responses in order; every request is kept for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        status, value = self.responses.pop(0)
        if isinstance(value, bytes):
            return {"status": status, "headers": {}, "body": value}
        return _json(status, value)


class FakeCloud:
    """Answers the three APIs from an in-memory copy of the reference double's state."""

    def __init__(self, state, *, pending_waits=1):
        self.service = copy.deepcopy(state["service"])
        self.revisions = copy.deepcopy(state["revisions"])
        self.queue = copy.deepcopy(state["queue"])
        self.tasks = copy.deepcopy(state["tasks"])
        self.objects = {
            name: {
                "generation": entry["generation"],
                "raw": base64.b64decode(entry["raw_b64"]),
            }
            for name, entry in state["objects"].items()
        }
        self.operations = {}
        self.pending_waits = pending_waits
        self.next_generation = int(state.get("next_generation", 100))
        self.calls = []

    def mutations(self):
        return [
            (call["method"], call["url"])
            for call in self.calls
            if call["method"] != "GET"
        ]

    def _operation(self, kind):
        name = SERVICE_API.rsplit("/services/", 1)[0]
        name += f"/operations/{kind}-{len(self.operations) + 1}"
        self.operations[name] = {"pending": self.pending_waits}
        return _json(200, {"name": name, "done": False})

    def __call__(self, request):
        self.calls.append(request)
        assert request["headers"]["Authorization"] == "Bearer " + TOKEN
        parsed = urllib.parse.urlsplit(request["url"])
        query = dict(urllib.parse.parse_qsl(parsed.query))
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        body = request.get("body")
        method = request["method"]
        if base.startswith(native.UPLOAD_ENDPOINT):
            return self._upload(base, query, body)
        if base.startswith(native.STORAGE_ENDPOINT):
            return self._storage(method, base, query)
        payload = json.loads(body) if body else None
        if base.startswith(native.RUN_ENDPOINT):
            return self._run(method, base[len(native.RUN_ENDPOINT) :], query, payload)
        if base.startswith(native.TASKS_ENDPOINT):
            return self._tasks(method, base[len(native.TASKS_ENDPOINT) :])
        raise AssertionError(request["url"])

    def _run(self, method, name, query, payload):
        if name.endswith(":wait") and method == "POST":
            operation = self.operations[name[:-5]]
            assert payload == {"timeout": "30s"}
            if operation["pending"] > 0:
                operation["pending"] -= 1
                return _json(200, {"name": name[:-5], "done": False})
            return _json(200, {"name": name[:-5], "done": True, "response": {}})
        if name == SERVICE_API and method == "GET":
            return _json(200, self.service)
        if name == SERVICE_API and method == "PATCH":
            if payload.get("etag") != self.service.get("etag"):
                return _json(409, {"error": {"status": "ABORTED"}})
            if query["updateMask"] == "template":
                template = payload["template"]
                full = SERVICE_API + "/revisions/" + template["revision"]
                self.revisions[full] = {
                    "name": full,
                    "serviceAccount": template["serviceAccount"],
                    "containers": copy.deepcopy(template["containers"]),
                    "timeout": template["timeout"],
                    "scaling": copy.deepcopy(template["scaling"]),
                    "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
                }
                self.service["template"] = copy.deepcopy(template)
                self.service["latestCreatedRevision"] = full
                self.service["etag"] = "etag-" + template["revision"]
                return self._operation("deploy")
            assert query["updateMask"] == "traffic"
            if payload["etag"] != self.service["etag"]:
                return _json(409, {"error": {"status": "ABORTED"}})
            self.service["traffic"] = copy.deepcopy(payload["traffic"])
            self.service["trafficStatuses"] = [
                {**item, "uri": "https://tagged.example"} for item in payload["traffic"]
            ]
            self.service["etag"] = "etag-routed"
            return self._operation("traffic")
        if method == "GET" and name in self.revisions:
            return _json(200, self.revisions[name])
        return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})

    def _tasks(self, method, name):
        if name == QUEUE_API + ":pause" and method == "POST":
            self.queue["state"] = "PAUSED"
            return _json(200, self.queue)
        if name == QUEUE_API + ":resume" and method == "POST":
            self.queue["state"] = "RUNNING"
            return _json(200, self.queue)
        if name == QUEUE_API and method == "GET":
            return _json(200, self.queue)
        if name == QUEUE_API + "/tasks" and method == "GET":
            return _json(200, {"tasks": self.tasks} if self.tasks else {})
        return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})

    def _storage(self, method, base, query):
        assert method == "GET"
        prefix = native.STORAGE_ENDPOINT + f"b/{release.BUCKET}/o/"
        assert base.startswith(prefix), base
        name = urllib.parse.unquote(base[len(prefix) :])
        stored = self.objects.get(name)
        if stored is None or (
            "generation" in query and query["generation"] != stored["generation"]
        ):
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        if query.get("alt") == "media":
            return {"status": 200, "headers": {}, "body": stored["raw"]}
        return _json(
            200,
            {
                "name": name,
                "generation": stored["generation"],
                "size": str(len(stored["raw"])),
            },
        )

    def _upload(self, base, query, body):
        assert base == native.UPLOAD_ENDPOINT + f"b/{release.BUCKET}/o"
        assert query["uploadType"] == "media"
        name = query["name"]
        stored = self.objects.get(name)
        current = int(stored["generation"]) if stored else 0
        if int(query["ifGenerationMatch"]) != current:
            return _json(412, {"error": {"code": 412, "status": "FAILED_PRECONDITION"}})
        generation = str(self.next_generation)
        self.next_generation += 1
        self.objects[name] = {"generation": generation, "raw": bytes(body)}
        return _json(200, {"name": name, "generation": generation})


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(native, "_gcloud_token", lambda: TOKEN)
    monkeypatch.setattr(native, "_adc_token", lambda: TOKEN)


def build(transport):
    return native.build("owner-gcloud", transport=transport)


def settled_pricing_service(state):
    service = copy.deepcopy(state["service"])
    revision = service["name"] + "/revisions/" + service["template"]["revision"]
    service["reconciling"] = False
    service["generation"] = "1"
    service["observedGeneration"] = "1"
    service["latestCreatedRevision"] = revision
    service["latestReadyRevision"] = revision
    return service


def live_window(scenario):
    """The native clock is real time, so the authority window must contain it."""
    write_json(
        scenario.authority_path,
        authority("release", expires_at=datetime.now(UTC) + timedelta(hours=2)),
    )
    return scenario


# Factory, method map, token source


def test_factory_returns_the_adapter_release_loads_without_minting_a_token(
    monkeypatch,
):
    def refuse(*_args, **_kwargs):
        raise AssertionError("token minted at load time")

    monkeypatch.setattr(native, "_run_command", refuse)
    clients = release._load_adapter(str(NATIVE_PATH) + ":factory", "owner-gcloud")
    assert set(clients) >= {"run", "tasks", "objects", "bytes", "clock", "_native"}
    for name, double in (
        ("run", fake._Run),
        ("tasks", fake._Tasks),
        ("objects", fake._Objects),
        ("bytes", fake._Bytes),
        ("clock", fake._Clock),
    ):
        expected = {
            method: inspect.signature(getattr(double, method))
            for method in dir(double)
            if not method.startswith("_") and callable(getattr(double, method))
        }
        actual = {
            method: inspect.signature(getattr(type(clients[name]), method))
            for method in dir(type(clients[name]))
            if not method.startswith("_")
            and callable(getattr(type(clients[name]), method))
        }
        assert actual == expected, name


def test_unknown_state_refuses_before_any_request():
    with pytest.raises(ValueError, match="adapter_state_invalid"):
        native.build("service-account-key", transport=FakeTransport([]))


def test_owner_gcloud_mints_the_token_through_the_absolute_gcloud_path(
    monkeypatch, tmp_path
):
    gcloud = tmp_path / "gcloud.cmd"
    gcloud.write_text("", encoding="utf-8")
    monkeypatch.setattr(native, "_gcloud_path", lambda: gcloud)
    calls = []

    def run_command(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"minted\n", stderr=b"")

    monkeypatch.setattr(native, "_run_command", run_command)
    transport = FakeTransport(
        [(200, {"name": SERVICE_API}), (200, {"name": QUEUE_API})]
    )
    clients = build(transport)
    assert clients["run"].get_service(SERVICE_API) == {"name": SERVICE_API}
    assert clients["tasks"].get_queue(QUEUE_API) == {"name": QUEUE_API}
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == str(gcloud) and Path(argv[0]).is_absolute()
    assert argv[1:4] == [
        "auth",
        "print-access-token",
        "--account=" + native.OWNER_ACCOUNT,
    ]
    assert kwargs["shell"] is False
    for request in transport.requests:
        assert request["headers"]["Authorization"] == "Bearer minted"
    assert "minted" not in json.dumps(clients["_native"].record)


def test_owner_gcloud_failures_refuse_by_code(monkeypatch, tmp_path):
    monkeypatch.setattr(native, "_gcloud_path", lambda: tmp_path / "missing.cmd")
    with pytest.raises(ValueError, match="gcloud_unavailable"):
        build(FakeTransport([]))["run"].get_service(SERVICE_API)
    gcloud = tmp_path / "gcloud.cmd"
    gcloud.write_text("", encoding="utf-8")
    monkeypatch.setattr(native, "_gcloud_path", lambda: gcloud)
    monkeypatch.setattr(
        native,
        "_run_command",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, b"", b"denied"),
    )
    with pytest.raises(ValueError, match="owner_token_unavailable"):
        build(FakeTransport([]))["run"].get_service(SERVICE_API)


def test_adc_state_uses_application_default_credentials(monkeypatch):
    monkeypatch.setattr(native, "_adc_token", lambda: "adc-minted")
    transport = FakeTransport([(200, {"name": SERVICE_API})])
    clients = native.build("adc", transport=transport)
    clients["run"].get_service(SERVICE_API)
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer adc-minted"


# Cloud Run


def test_get_service_and_revision_map_status_to_value_none_or_error(token):
    revision = SERVICE_API + "/revisions/listening-post-staging-q-new"
    transport = FakeTransport(
        [
            (200, {"name": SERVICE_API, "etag": "e1"}),
            (404, {"error": {"code": 404}}),
            (403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}}),
        ]
    )
    clients = build(transport)
    assert clients["run"].get_service(SERVICE_API) == {
        "name": SERVICE_API,
        "etag": "e1",
    }
    assert clients["run"].get_revision(revision) is None
    with pytest.raises(native.NativeError, match="403"):
        clients["run"].get_service(SERVICE_API)
    assert [(r["method"], r["url"]) for r in transport.requests] == [
        ("GET", native.RUN_ENDPOINT + SERVICE_API),
        ("GET", native.RUN_ENDPOINT + revision),
        ("GET", native.RUN_ENDPOINT + SERVICE_API),
    ]
    assert [entry["call"] for entry in clients["_native"].record] == [
        "get_service",
        "get_revision",
        "get_service",
    ]


def test_deploy_revision_patches_the_exact_template_without_traffic(token, tmp_path):
    scenario = Scenario(tmp_path)
    template = release.expected_template(scenario.binding, scenario.tag)
    transport = FakeTransport(
        [
            (200, scenario.state["service"]),
            (200, {"name": OPERATION, "done": False}),
        ]
    )
    clients = build(transport)
    result = clients["run"].deploy_revision(
        SERVICE_API,
        revision=scenario.binding["revision_name"],
        template=template,
        idempotency_key="k" * 64,
    )
    assert result == {"operation": OPERATION}
    assert transport.requests[0]["method"] == "GET"
    request = transport.requests[1]
    assert request["method"] == "PATCH"
    assert request["url"] == native.RUN_ENDPOINT + SERVICE_API + "?updateMask=template"
    assert json.loads(request["body"]) == {
        "template": template,
        "etag": scenario.state["service"]["etag"],
    }
    assert request["headers"]["Content-Type"] == "application/json"
    entry = clients["_native"].record[1]
    assert entry["call"] == "deploy_revision"
    assert entry["idempotency_key"] == "k" * 64
    assert entry["request"] == release.redacted_service(
        {"template": template, "etag": scenario.state["service"]["etag"]}
    )
    assert entry["request"]["template"]["containers"][0]["env_names"] == sorted(
        item["name"] for item in template["containers"][0]["env"]
    )
    assert entry["response"] == {"name": OPERATION, "done": False}
    with pytest.raises(ValueError, match="template_revision_mismatch"):
        clients["run"].deploy_revision(
            SERVICE_API, revision="other", template=template, idempotency_key="k"
        )
    assert len(transport.requests) == 2


def test_pricing_deploy_reads_service_etag_and_preserves_image_and_environment(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    service = settled_pricing_service(scenario.state)
    template = copy.deepcopy(service["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template["revision"] = revision
    digest = next(
        entry
        for entry in template["containers"][0]["env"]
        if entry["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
    )
    digest["value"] = "new-reviewed-binding"
    transport = FakeTransport(
        [(200, service), (200, {"name": OPERATION, "done": False})]
    )
    clients = build(transport)

    result = clients["run"].deploy_revision(
        SERVICE_API,
        revision=revision,
        template=template,
        idempotency_key="k" * 64,
        expected_current_template=service["template"],
        expected_current_service=service,
    )

    assert result == {"operation": OPERATION}
    assert [(item["method"], item["url"]) for item in transport.requests] == [
        ("GET", native.RUN_ENDPOINT + SERVICE_API),
        ("PATCH", native.RUN_ENDPOINT + SERVICE_API + "?updateMask=template"),
    ]
    assert json.loads(transport.requests[1]["body"]) == {
        "template": template,
        "etag": service["etag"],
    }
    assert [entry["call"] for entry in clients["_native"].record] == [
        "get_service",
        "deploy_revision",
    ]


@pytest.mark.parametrize(
    "drift", ["image", "env", "bridge_reads", "new_env", "missing_etag"]
)
def test_pricing_deploy_refuses_drift_before_service_patch(token, tmp_path, drift):
    scenario = Scenario(tmp_path)
    service = settled_pricing_service(scenario.state)
    template = copy.deepcopy(service["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template["revision"] = revision
    if drift == "image":
        template["containers"][0]["image"] = "changed-image"
    elif drift == "env":
        next(
            entry
            for entry in template["containers"][0]["env"]
            if entry["name"] == "SOURCE_SHA"
        )["value"] = "changed-source"
    elif drift == "bridge_reads":
        next(
            entry
            for entry in template["containers"][0]["env"]
            if entry["name"] == "GENERAL_QUESTION_BRIDGE_READS"
        )["value"] = "changed-switch"
    elif drift == "new_env":
        template["containers"][0]["env"].append(
            {"name": "UNREVIEWED", "value": "changed"}
        )
    else:
        service.pop("etag")
    transport = FakeTransport([(200, service)])
    clients = build(transport)

    with pytest.raises(ValueError, match="service_etag_missing|pricing_service_drift"):
        clients["run"].deploy_revision(
            SERVICE_API,
            revision=revision,
            template=template,
            idempotency_key="k" * 64,
            expected_current_template=service["template"],
            expected_current_service=service,
        )

    assert [(item["method"], item["url"]) for item in transport.requests] == [
        ("GET", native.RUN_ENDPOINT + SERVICE_API)
    ]


def test_concurrent_service_change_refuses_pricing_deploy_without_a_write(
    token, tmp_path
):
    scenario = Scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    cloud.service = settled_pricing_service(scenario.state)
    original = copy.deepcopy(cloud.service)
    template = copy.deepcopy(original["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template["revision"] = revision
    run = cloud._run

    def concurrent(method, name, query, payload):
        if method == "PATCH" and query.get("updateMask") == "template":
            cloud.service["etag"] = "etag-from-concurrent-update"
        return run(method, name, query, payload)

    cloud._run = concurrent
    clients = build(cloud)

    with pytest.raises(native.NativeError, match="deploy_revision: http 409"):
        clients["run"].deploy_revision(
            SERVICE_API,
            revision=revision,
            template=template,
            idempotency_key="k" * 64,
            expected_current_template=original["template"],
            expected_current_service=original,
        )

    assert cloud.service["template"] == original["template"]
    assert cloud.service["traffic"] == original["traffic"]
    assert cloud.revisions == scenario.state["revisions"]
    patches = [item for item in cloud.calls if item["method"] == "PATCH"]
    assert len(patches) == 1
    assert json.loads(patches[0]["body"])["etag"] == original["etag"]


@pytest.mark.parametrize(
    "drift",
    [
        "service_account",
        "timeout",
        "scaling",
        "cpu",
        "memory",
        "concurrency",
        "ports",
        "startup_probe",
        "volumes",
        "old_pointer",
    ],
)
def test_pricing_deploy_refuses_native_template_change_after_prior_read(
    token, tmp_path, drift
):
    scenario = Scenario(tmp_path)
    service = settled_pricing_service(scenario.state)
    old_service = copy.deepcopy(service)
    old_template = copy.deepcopy(service["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template = copy.deepcopy(old_template)
    template["revision"] = revision
    next(
        entry
        for entry in template["containers"][0]["env"]
        if entry["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
    )["value"] = "new-reviewed-binding"
    observed = service["template"]
    if drift == "service_account":
        observed["serviceAccount"] = "different@example.invalid"
    elif drift == "timeout":
        observed["timeout"] = "500s"
    elif drift == "scaling":
        observed["scaling"]["maxInstanceCount"] = 2
    elif drift == "cpu":
        observed["containers"][0]["resources"]["limits"]["cpu"] = "4"
    elif drift == "memory":
        observed["containers"][0]["resources"]["limits"]["memory"] = "8Gi"
    elif drift == "concurrency":
        observed["maxInstanceRequestConcurrency"] = 160
    elif drift == "ports":
        observed["containers"][0]["ports"] = [{"containerPort": 8181}]
    elif drift == "startup_probe":
        observed["containers"][0]["startupProbe"] = {"tcpSocket": {"port": 8181}}
    elif drift == "volumes":
        observed["volumes"] = [{"name": "unexpected"}]
    else:
        next(
            entry
            for entry in observed["containers"][0]["env"]
            if entry["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
        )["value"] = "concurrent-old-pointer"
    transport = FakeTransport([(200, service)])
    clients = build(transport)

    with pytest.raises(ValueError, match="pricing_service_drift"):
        clients["run"].deploy_revision(
            SERVICE_API,
            revision=revision,
            template=template,
            idempotency_key="k" * 64,
            expected_current_template=old_template,
            expected_current_service=old_service,
        )

    assert [(item["method"], item["url"]) for item in transport.requests] == [
        ("GET", native.RUN_ENDPOINT + SERVICE_API)
    ]


def test_pricing_deploy_requires_an_independent_expected_template(token, tmp_path):
    scenario = Scenario(tmp_path)
    service = copy.deepcopy(scenario.state["service"])
    template = copy.deepcopy(service["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template["revision"] = revision
    transport = FakeTransport([(200, service)])
    clients = build(transport)

    with pytest.raises(ValueError, match="pricing_expected_template_missing"):
        clients["run"].deploy_revision(
            SERVICE_API,
            revision=revision,
            template=template,
            idempotency_key="k" * 64,
        )

    assert all(item["method"] != "PATCH" for item in transport.requests)


def test_pricing_deploy_requires_the_verified_serving_state(token, tmp_path):
    scenario = Scenario(tmp_path)
    service = copy.deepcopy(scenario.state["service"])
    template = copy.deepcopy(service["template"])
    revision = release.SERVICE_NAME + "-p-newpolicy"
    template["revision"] = revision
    transport = FakeTransport([(200, service)])
    clients = build(transport)

    with pytest.raises(ValueError, match="pricing_expected_service_missing"):
        clients["run"].deploy_revision(
            SERVICE_API,
            revision=revision,
            template=template,
            idempotency_key="k" * 64,
            expected_current_template=service["template"],
        )

    assert all(item["method"] != "PATCH" for item in transport.requests)


@pytest.mark.parametrize(
    "drift",
    [
        "none",
        "etag",
        "template",
        "reconciling",
        "traffic",
        "traffic_status",
        "unobserved",
        "latest_ready",
        "not_ready",
    ],
)
def test_pricing_caller_second_service_read_must_still_be_settled(
    token, tmp_path, drift
):
    from ops.tests.test_refresh_question_policy_state_guard import (
        Lane,
        _clients,
        _renew,
        _use_realistic_page,
    )

    lane = Lane(tmp_path)
    observed = _use_realistic_page(lane)
    clients = _clients(lane)
    original_run = clients["run"]
    current = original_run.get_service(release.SERVICE_API_NAME)
    fresh = copy.deepcopy(current)
    if drift == "etag":
        fresh["etag"] = "changed-service-etag"
    elif drift == "template":
        fresh["template"]["maxInstanceRequestConcurrency"] = 160
    elif drift == "reconciling":
        fresh["reconciling"] = True
    elif drift == "traffic":
        fresh["traffic"][0]["revision"] = "unreviewed-revision"
    elif drift == "traffic_status":
        fresh["trafficStatuses"][0]["revision"] = "unreviewed-revision"
    elif drift == "unobserved":
        fresh["generation"] = str(int(fresh["generation"]) + 1)
    elif drift == "latest_ready":
        fresh["latestReadyRevision"] = "unreviewed-revision"
    elif drift == "not_ready":
        fresh["terminalCondition"]["state"] = "CONDITION_FAILED"
    transport = FakeTransport([(200, fresh), (200, {"name": OPERATION})])
    native_run = build(transport)["run"]
    captured = []

    class CallerRun:
        def __getattr__(self, name):
            return getattr(original_run, name)

        def deploy_revision(self, service_name, **kwargs):
            captured.append(copy.deepcopy(kwargs))
            native_run.deploy_revision(service_name, **kwargs)
            return original_run.deploy_revision(service_name, **kwargs)

    clients["run"] = CallerRun()
    receipt = _renew(lane, clients, observed)

    assert captured[0]["expected_current_template"] == current["template"]
    patches = [item for item in transport.requests if item["method"] == "PATCH"]
    if drift == "none":
        assert receipt["state"] == "activated", receipt
        assert len(patches) == 1
        assert json.loads(patches[0]["body"])["etag"] == fresh["etag"]
    else:
        assert patches == [], (drift, receipt)


def test_update_traffic_routes_the_named_revision_under_the_etag(token, tmp_path):
    scenario = Scenario(tmp_path)
    traffic = release.traffic_targets(scenario.binding, scenario.tag)
    transport = FakeTransport([(200, {"name": OPERATION, "done": False})])
    clients = build(transport)
    result = clients["run"].update_traffic(
        SERVICE_API, etag="etag-before", traffic=traffic, idempotency_key="k" * 64
    )
    assert result == {"operation": OPERATION}
    request = transport.requests[0]
    assert request["method"] == "PATCH"
    assert request["url"] == native.RUN_ENDPOINT + SERVICE_API + "?updateMask=traffic"
    assert json.loads(request["body"]) == {"traffic": traffic, "etag": "etag-before"}
    assert traffic[0]["percent"] == 100


def test_mutation_error_status_raises_and_is_recorded(token):
    transport = FakeTransport([(409, {"error": {"status": "ABORTED"}})])
    clients = build(transport)
    with pytest.raises(native.NativeError, match="409"):
        clients["run"].update_traffic(
            SERVICE_API, etag="stale", traffic=[], idempotency_key="k"
        )
    assert clients["_native"].record[0]["status"] == 409


def test_read_operation_waits_and_maps_done_and_error(token):
    transport = FakeTransport(
        [
            (200, {"name": OPERATION, "done": False}),
            (200, {"name": OPERATION, "done": True, "response": {}}),
            (
                200,
                {
                    "name": OPERATION,
                    "done": True,
                    "error": {"code": 9, "message": "revision failed"},
                },
            ),
            (200, {"name": "projects/x/operations/other", "done": True}),
            (500, {"error": {"code": 500}}),
        ]
    )
    clients = build(transport)
    read = clients["run"].read_operation
    assert read(OPERATION, 30.0) == {
        "operation": OPERATION,
        "state": "pending",
        "error": None,
    }
    assert read(OPERATION, 12.5) == {
        "operation": OPERATION,
        "state": "succeeded",
        "error": None,
    }
    assert read(OPERATION, 30.0) == {
        "operation": OPERATION,
        "state": "failed",
        "error": "9: revision failed",
    }
    assert read(OPERATION, 30.0)["operation"] == "projects/x/operations/other"
    with pytest.raises(native.NativeError, match="500"):
        read(OPERATION, 30.0)
    first, second = transport.requests[:2]
    assert first["method"] == "POST"
    assert first["url"] == native.RUN_ENDPOINT + OPERATION + ":wait"
    assert json.loads(first["body"]) == {"timeout": "30s"}
    assert json.loads(second["body"]) == {"timeout": "12.5s"}


# Cloud Tasks


def test_queue_calls_use_the_v2_paths_and_bodies(token):
    queue = {"name": QUEUE_API, "state": "RUNNING"}
    transport = FakeTransport(
        [
            (200, queue),
            (200, {}),
            (200, {"tasks": [{"name": QUEUE_API + "/tasks/t1"}], "nextPageToken": "n"}),
            (200, {**queue, "state": "PAUSED"}),
            (200, queue),
            (404, {"error": {"code": 404}}),
        ]
    )
    clients = build(transport)
    tasks = clients["tasks"]
    assert tasks.get_queue(QUEUE_API) == queue
    assert tasks.list_tasks(QUEUE_API) == {}
    assert tasks.list_tasks(QUEUE_API, page_token="n")["nextPageToken"] == "n"
    assert tasks.pause(QUEUE_API)["state"] == "PAUSED"
    assert tasks.resume(QUEUE_API)["state"] == "RUNNING"
    assert tasks.get_queue(QUEUE_API) is None
    requests = transport.requests
    assert (requests[0]["method"], requests[0]["url"]) == (
        "GET",
        native.TASKS_ENDPOINT + QUEUE_API,
    )
    assert requests[1]["url"] == (
        native.TASKS_ENDPOINT + QUEUE_API + "/tasks?responseView=BASIC&pageSize=1000"
    )
    assert requests[2]["url"].endswith("&pageToken=n")
    assert (requests[3]["method"], requests[3]["url"]) == (
        "POST",
        native.TASKS_ENDPOINT + QUEUE_API + ":pause",
    )
    assert json.loads(requests[3]["body"]) == {}
    assert (requests[4]["method"], requests[4]["url"]) == (
        "POST",
        native.TASKS_ENDPOINT + QUEUE_API + ":resume",
    )


# Storage


def test_object_read_carries_the_creation_time_the_metadata_names(token):
    raw = b'{"contract_version":"42_release_index_v1"}'
    transport = FakeTransport(
        [
            (
                200,
                {
                    "name": LEDGER,
                    "generation": "40",
                    "size": str(len(raw)),
                    "timeCreated": "2026-09-25T07:00:00.123Z",
                },
            ),
            (200, raw),
        ]
    )
    clients = build(transport)
    assert clients["objects"].read(LEDGER) == {
        "generation": "40",
        "raw": raw,
        "time_created": "2026-09-25T07:00:00.123Z",
    }


def test_object_read_pins_the_generation_it_saw(token):
    raw = b'{"contract_version":"general_question_allowance_v1"}'
    quoted = urllib.parse.quote(LEDGER, safe="")
    transport = FakeTransport(
        [
            (200, {"name": LEDGER, "generation": "40", "size": str(len(raw))}),
            (200, raw),
            (404, {"error": {"code": 404}}),
            (200, {"name": LEDGER, "generation": "41", "size": str(len(raw))}),
            (200, raw),
            (200, {"name": LEDGER, "generation": "42", "size": "999"}),
            (200, raw),
        ]
    )
    clients = build(transport)
    assert clients["objects"].read(LEDGER) == {"generation": "40", "raw": raw}
    assert clients["objects"].read(LEDGER, generation="39") is None
    assert clients["objects"].read(LEDGER, generation=41)["generation"] == "41"
    with pytest.raises(native.NativeError, match="object_size_mismatch"):
        clients["objects"].read(LEDGER)
    urls = [request["url"] for request in transport.requests]
    base = native.STORAGE_ENDPOINT + f"b/{release.BUCKET}/o/{quoted}"
    assert urls[0] == base
    assert urls[1] == base + "?alt=media&generation=40"
    assert urls[2] == base + "?generation=39"
    assert urls[3] == base + "?generation=41"
    assert urls[4] == base + "?alt=media&generation=41"
    assert all(request["method"] == "GET" for request in transport.requests)
    media = clients["_native"].record[1]
    assert media["response"] == {"sha256": release._sha256(raw), "bytes": len(raw)}


def test_object_create_sends_the_generation_precondition(token):
    raw = b'{"a":1}'
    transport = FakeTransport(
        [
            (200, {"name": LEDGER, "generation": "100"}),
            (412, {"error": {"code": 412, "status": "FAILED_PRECONDITION"}}),
            (500, {"error": {"code": 500}}),
        ]
    )
    clients = build(transport)
    assert clients["objects"].create(LEDGER, raw, if_generation_match=0) == {
        "generation": "100"
    }
    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == (
        native.UPLOAD_ENDPOINT
        + f"b/{release.BUCKET}/o?uploadType=media&name="
        + urllib.parse.quote(LEDGER, safe="")
        + "&ifGenerationMatch=0"
    )
    assert request["body"] == raw
    assert request["headers"]["Content-Type"] == "application/json"
    with pytest.raises(native.PreconditionFailed, match="generation_mismatch"):
        clients["objects"].create(LEDGER, raw, if_generation_match="40")
    assert transport.requests[1]["url"].endswith("&ifGenerationMatch=40")
    with pytest.raises(native.NativeError, match="500"):
        clients["objects"].create(LEDGER, raw, if_generation_match=40)
    entry = clients["_native"].record[0]
    assert entry["request"] == {"sha256": release._sha256(raw), "bytes": len(raw)}
    assert entry["if_generation_match"] == 0


def test_bytes_reader_resolves_object_references_only(token):
    """The reference must name the approved evidence bucket. This test used to pin
    that the reader would fetch an object from any bucket the reference named,
    which is the weakness the hostile review found; the approved bucket is now the
    only one it will spend the owner credential on."""
    raw = b"manifest"
    transport = FakeTransport(
        [
            (200, {"name": "x/y.json", "generation": "7", "size": "8"}),
            (200, raw),
            (404, {"error": {"code": 404}}),
        ]
    )
    clients = build(transport)
    approved = f"object:gs://{release.BUCKET}/x/y.json"
    assert clients["bytes"].read(approved + "#7") == raw
    assert transport.requests[0]["url"] == (
        native.STORAGE_ENDPOINT + f"b/{release.BUCKET}/o/x%2Fy.json?generation=7"
    )
    assert clients["bytes"].read(approved + "#8") is None
    assert clients["bytes"].read("app_image") is None
    assert clients["bytes"].read("object:not-a-uri") is None
    with pytest.raises(ValueError, match="object_bucket_not_approved"):
        clients["bytes"].read("object:gs://other-bucket/x/y.json#7")
    assert len(transport.requests) == 3


def test_clock_is_aware_and_monotonic(token):
    clock = build(FakeTransport([]))["clock"]
    now = clock.now()
    assert isinstance(now, datetime) and now.tzinfo is not None
    assert isinstance(clock.monotonic(), float)
    clock.sleep(0)


def test_render_shows_the_request_without_sending(token):
    clients = build(FakeTransport([]))
    rendered = clients["_native"].render("pause_queue", name=QUEUE_API)
    assert rendered == {
        "call": "pause_queue",
        "method": "POST",
        "url": native.TASKS_ENDPOINT + QUEUE_API + ":pause",
        "body": {},
    }
    rendered = clients["_native"].render(
        "create_object", name=LEDGER, raw=b"{}", if_generation_match=40
    )
    assert rendered["method"] == "POST"
    assert rendered["url"].endswith("&ifGenerationMatch=40")
    assert rendered["body"] == {"sha256": release._sha256(b"{}"), "bytes": 2}
    assert clients["_native"].record == []


# release.py plan, apply and verify against the native adapter over the fake cloud


def test_release_plan_apply_verify_run_against_the_native_adapter(token, tmp_path):
    scenario = live_window(Scenario(tmp_path))
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    common = [
        "--adapter",
        "native:factory",
        "--resources",
        str(MANIFEST_PATH),
        "--poll-seconds",
        "0.01",
    ]
    plan_path = tmp_path / "release-plan.json"
    code = release.execute(
        [
            "plan",
            "--activation",
            str(scenario.activation_path),
            "--authority",
            str(scenario.authority_path),
            "--output",
            str(plan_path),
            *common,
        ],
        clients=clients,
    )
    assert code == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    template = release.expected_template(scenario.binding, scenario.tag)
    assert [change["operation"] for change in plan["changes"]] == [
        "deploy_revision",
        "route_traffic",
    ]
    assert plan["changes"][0]["template"] == template
    assert plan["before"]["ledger"]["generation"] == "40"
    assert cloud.mutations() == []

    result_path = tmp_path / "release-result.json"
    code = release.execute(
        ["apply", "--plan", str(plan_path), "--output", str(result_path), *common],
        clients=clients,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert (code, result["state"]) == (0, release.ROUTE_VERIFIED_STATE), result
    targets = release.traffic_targets(scenario.binding, scenario.tag)
    service_url = native.RUN_ENDPOINT + SERVICE_API
    deploy_op = result["operations"]["deploy_revision"]["operation"]
    route_op = result["operations"]["route_traffic"]["operation"]
    assert cloud.mutations() == [
        ("PATCH", service_url + "?updateMask=template"),
        ("POST", native.RUN_ENDPOINT + deploy_op + ":wait"),
        ("POST", native.RUN_ENDPOINT + deploy_op + ":wait"),
        ("PATCH", service_url + "?updateMask=traffic"),
        ("POST", native.RUN_ENDPOINT + route_op + ":wait"),
        ("POST", native.RUN_ENDPOINT + route_op + ":wait"),
    ]
    patches = [call for call in cloud.calls if call["method"] == "PATCH"]
    assert json.loads(patches[0]["body"]) == {
        "template": template,
        "etag": scenario.state["service"]["etag"],
    }
    assert json.loads(patches[1]["body"]) == {
        "traffic": targets,
        "etag": "etag-" + scenario.binding["revision_name"],
    }
    assert cloud.service["template"] == template
    assert cloud.service["traffic"] == targets
    assert result["operations"]["deploy_revision"]["reads"] == 2
    assert result["after"]["ledger"]["generation"] == "40"

    verified_path = tmp_path / "release-verified.json"
    code = release.execute(
        [
            "verify",
            "--plan",
            str(plan_path),
            "--result",
            str(result_path),
            "--output",
            str(verified_path),
            *common,
        ],
        clients=clients,
    )
    verified = json.loads(verified_path.read_text(encoding="utf-8"))
    assert (code, verified["state"], verified["drift"]) == (0, "verified", [])
    record = clients["_native"].record
    assert TOKEN not in json.dumps(record)
    assert [entry["call"] for entry in record if entry["call"] == "deploy_revision"]
    assert all(
        {"call", "method", "url", "status", "response"} <= set(e) for e in record
    )


def test_apply_reports_a_failed_operation_without_polling_it_to_success(
    token, tmp_path
):
    scenario = live_window(Scenario(tmp_path))
    cloud = FakeCloud(scenario.state, pending_waits=0)
    clients = build(cloud)
    common = [
        "--adapter",
        "native:factory",
        "--resources",
        str(MANIFEST_PATH),
        "--poll-seconds",
        "0.01",
    ]
    plan_path = tmp_path / "release-plan.json"
    release.execute(
        [
            "plan",
            "--activation",
            str(scenario.activation_path),
            "--authority",
            str(scenario.authority_path),
            "--output",
            str(plan_path),
            *common,
        ],
        clients=clients,
    )
    original = cloud._run

    def failing(method, name, query, payload):
        response = original(method, name, query, payload)
        if name.endswith(":wait"):
            body = json.loads(response["body"])
            body["error"] = {"code": 13, "message": "container failed to start"}
            return _json(200, body)
        return response

    cloud._run = failing
    result_path = tmp_path / "release-result.json"
    code = release.execute(
        ["apply", "--plan", str(plan_path), "--output", str(result_path), *common],
        clients=clients,
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 1
    assert result["state"] == "failed_or_unproven_requires_native_reconciliation"
    assert result["error"] == "deployment_failed"
    assert result["operations"]["deploy_revision"]["last_error"] == (
        "13: container failed to start"
    )
    assert [m for m in cloud.mutations() if m[1].endswith("updateMask=traffic")] == []


def test_release_cli_loads_the_native_adapter_and_refuses_an_unknown_state(tmp_path):
    scenario = Scenario(tmp_path)
    code, summary, stderr = run_cli(
        "plan",
        "--activation",
        scenario.activation_path,
        "--authority",
        scenario.authority_path,
        "--output",
        tmp_path / "release-plan.json",
        "--adapter",
        str(NATIVE_PATH) + ":factory",
        "--adapter-state",
        "service-account-key",
        "--resources",
        MANIFEST_PATH,
    )
    assert (code, summary["error"]) == (1, "adapter_state_invalid"), stderr
    assert not (tmp_path / "release-plan.json").exists()


def test_record_redacts_private_environment_values_but_not_the_wire(token, tmp_path):
    scenario = Scenario(tmp_path)
    service = copy.deepcopy(scenario.state["service"])
    service["template"]["containers"][0]["env"].append(
        {"name": "OPERATOR_NOTE", "value": "private-note"}
    )
    service["template"]["containers"][0]["env"].append(
        {"name": "UI_PASSCODE", "valueSource": {"secretKeyRef": {"secret": "s"}}}
    )
    revision = {
        "name": SERVICE_API + "/revisions/r",
        "containers": service["template"]["containers"],
    }
    transport = FakeTransport([(200, service), (200, revision)])
    clients = build(transport)
    assert clients["run"].get_service(SERVICE_API) == service
    assert clients["run"].get_revision(revision["name"]) == revision
    recorded = json.dumps(clients["_native"].record)
    assert "private-ttl" not in recorded
    assert "secretKeyRef" not in recorded
    env = clients["_native"].record[1]["response"]["containers"][0]["env"]
    assert {"name": "UI_PASSCODE", "source": "secret"} in env
    assert any(item["name"] == "OPERATOR_NOTE" and "value_sha256" in item for item in env)


# The whole lifecycle over the native adapter: deploy, readback, resume, rollback

COMMON = ["--adapter", "native:factory", "--poll-seconds", "0.01"]


@pytest.fixture
def native_offset(monkeypatch):
    """The adapter clock with a test-held offset, so a real-time drain can elapse."""
    offset = {"seconds": 0.0}
    monkeypatch.setattr(
        native._Clock,
        "now",
        lambda self: datetime.now(UTC) + timedelta(seconds=offset["seconds"]),
    )
    return offset


def lifecycle_scenario(tmp_path):
    """A release scenario whose authority windows contain the adapter's real clock."""
    policy = make_policy()
    binding = make_binding(policy, revision=SERVICE + "-q-new", image_digest=NEW_IMAGE)
    scenario = live_window(
        Scenario(tmp_path, ledger_requests=terminal_requests(policy, binding))
    )
    scenario.rollback_authority = write_json(
        tmp_path / "rollback-authority.json",
        authority("rollback", expires_at=datetime.now(UTC) + timedelta(hours=2)),
    )
    return scenario


def run_native(clients, *args):
    return release.execute(
        [*[str(item) for item in args], "--resources", str(MANIFEST_PATH), *COMMON],
        clients=clients,
    )


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ledger_of(cloud):
    return json.loads(cloud.objects[LEDGER]["raw"].decode("utf-8"))


def released(scenario, cloud, clients, offset):
    """plan, apply and verify over the native adapter, then let the drain elapse."""
    plan_path = scenario.dir / "release-plan.json"
    result_path = scenario.dir / "release-result.json"
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 0
    )
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 0
    )
    assert read(verified_path)["drift"] == []
    offset["seconds"] += 200
    return result_path


def test_native_resume_runs_the_queue_through_the_tasks_v2_resume_request(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    route_path = released(scenario, cloud, clients, native_offset)
    assert cloud.queue["state"] == "PAUSED"
    mutations_before = len(cloud.mutations())

    plan_path = scenario.dir / "resume-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--kind",
            "resume",
            "--mode",
            "empty",
            "--route",
            route_path,
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    assert read(plan_path)["resume"]["mode"] == "empty"
    assert len(cloud.mutations()) == mutations_before

    result_path = scenario.dir / "resume-result.json"
    assert (
        run_native(clients, "resume", "--plan", plan_path, "--output", result_path) == 0
    )
    result = read(result_path)
    assert result["state"] == "running_verified"
    assert result["drain_seconds"] >= 125
    assert cloud.queue["state"] == "RUNNING"
    assert cloud.mutations()[mutations_before:] == [
        ("POST", native.TASKS_ENDPOINT + QUEUE_API + ":resume")
    ]
    resumed = [
        call for call in cloud.calls if call["url"].endswith(QUEUE_API + ":resume")
    ]
    assert len(resumed) == 1
    assert json.loads(resumed[0]["body"]) == {}
    assert resumed[0]["headers"]["Content-Type"] == "application/json"
    assert [entry["call"] for entry in clients["_native"].record].count(
        "resume_queue"
    ) == 1


def test_native_rollback_swaps_only_the_active_binding_and_keeps_every_request(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    released(scenario, cloud, clients, native_offset)
    ledger_before = ledger_of(cloud)
    generation_before = cloud.objects[LEDGER]["generation"]
    mutations_before = len(cloud.mutations())

    plan_path = scenario.dir / "rollback-plan.json"
    assert (
        run_native(
            clients,
            "rollback-plan",
            "--target-revision",
            SERVICE + "-q-old",
            "--authority",
            scenario.rollback_authority,
            "--output",
            plan_path,
        )
        == 0
    )
    plan = read(plan_path)
    assert plan["target"]["revision_name"] == SERVICE + "-q-old"
    assert plan["classification"]["completed"] == [RID]
    assert plan["classification"]["held"] == [PARENT]
    assert [change["operation"] for change in plan["changes"]] == [
        "activate_binding",
        "route_traffic",
    ]
    assert len(cloud.mutations()) == mutations_before

    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 0
    )
    result = read(result_path)
    assert result["state"] == "rollback_verified_queue_paused"
    ledger_after = ledger_of(cloud)
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert ledger_after["reserved_microusd"] == ledger_before["reserved_microusd"]
    assert ledger_after["bindings"] == ledger_before["bindings"]
    assert (
        ledger_after["active_deployment_digest"]
        == scenario.old_binding["deployment_digest"]
    )
    assert cloud.objects[LEDGER]["generation"] != generation_before
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-old"
    assert cloud.service["template"]["revision"] == SERVICE + "-q-new"
    assert cloud.queue["state"] == "PAUSED"

    upload = native.UPLOAD_ENDPOINT + f"b/{release.BUCKET}/o"
    kinds = [
        (method, url.split("?")[0])
        for method, url in cloud.mutations()[mutations_before:]
    ]
    assert kinds[0] == ("POST", upload)
    assert kinds[1] == ("PATCH", native.RUN_ENDPOINT + SERVICE_API)
    ledger_write = [call for call in cloud.calls if call["url"].startswith(upload)][-1]
    assert f"ifGenerationMatch={generation_before}" in ledger_write["url"]
    assert json.loads(ledger_write["body"]) == ledger_after
    assert result["activation"]["if_generation_match"] == int(generation_before)


def test_native_rollback_refuses_a_concurrent_ledger_write_before_routing(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    released(scenario, cloud, clients, native_offset)
    ledger_before = ledger_of(cloud)
    generation_before = cloud.objects[LEDGER]["generation"]

    plan_path = scenario.dir / "rollback-plan.json"
    assert (
        run_native(
            clients,
            "rollback-plan",
            "--target-revision",
            SERVICE + "-q-old",
            "--authority",
            scenario.rollback_authority,
            "--output",
            plan_path,
        )
        == 0
    )
    original = cloud._upload

    def taken(base, query, body):
        """Another writer moved the ledger between the plan read and this write."""
        cloud._upload = original
        return _json(412, {"error": {"code": 412, "status": "FAILED_PRECONDITION"}})

    cloud._upload = taken
    mutations_before = len(cloud.mutations())
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["state"] == "refused"
    assert result["error"] == "stale_ledger_generation"
    assert result["activation"]["error"] == (
        "PreconditionFailed: create_object: http 412: generation_mismatch"
    )
    assert "route_traffic" not in result["operations"]
    assert set(result["forward_recovery_plan"]["preserved_requests"]) == {RID, PARENT}
    assert ledger_of(cloud) == ledger_before
    assert cloud.objects[LEDGER]["generation"] == generation_before
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    assert cloud.queue["state"] == "PAUSED"
    assert [
        (method, url.split("?")[0])
        for method, url in cloud.mutations()[mutations_before:]
    ] == [("POST", native.UPLOAD_ENDPOINT + f"b/{release.BUCKET}/o")]


def test_native_lifecycle_runs_deploy_readback_resume_rollback_and_resume_in_order(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    route_path = released(scenario, cloud, clients, native_offset)
    ledger_before = ledger_of(cloud)

    resume_plan = scenario.dir / "resume-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--kind",
            "resume",
            "--mode",
            "empty",
            "--route",
            route_path,
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            resume_plan,
        )
        == 0
    )
    resume_result = scenario.dir / "resume-result.json"
    assert (
        run_native(clients, "resume", "--plan", resume_plan, "--output", resume_result)
        == 0
    )
    assert cloud.queue["state"] == "RUNNING"

    rollback_plan = scenario.dir / "rollback-plan.json"
    assert (
        run_native(
            clients,
            "rollback-plan",
            "--target-revision",
            SERVICE + "-q-old",
            "--authority",
            scenario.rollback_authority,
            "--output",
            rollback_plan,
        )
        == 0
    )
    assert [change["operation"] for change in read(rollback_plan)["changes"]] == [
        "pause_queue",
        "activate_binding",
        "route_traffic",
    ]
    rollback_result = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients,
            "rollback-apply",
            "--plan",
            rollback_plan,
            "--output",
            rollback_result,
        )
        == 0
    )
    assert read(rollback_result)["queue_paused"]["state"] == "PAUSED"
    assert cloud.queue["state"] == "PAUSED"

    native_offset["seconds"] += 200
    after_plan = scenario.dir / "resume-after-rollback-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--kind",
            "resume",
            "--mode",
            "empty",
            "--route",
            rollback_result,
            "--authority",
            scenario.authority_path,
            "--output",
            after_plan,
        )
        == 0
    )
    plan = read(after_plan)
    assert plan["route"]["kind"] == "rollback"
    assert plan["expected"]["revision_name"] == SERVICE + "-q-old"
    after_result = scenario.dir / "resume-after-rollback-result.json"
    assert (
        run_native(clients, "resume", "--plan", after_plan, "--output", after_result)
        == 0
    )
    assert read(after_result)["state"] == "running_verified"
    assert cloud.queue["state"] == "RUNNING"

    ledger_after = ledger_of(cloud)
    assert canonical(ledger_after["requests"]) == canonical(ledger_before["requests"])
    assert ledger_after["reserved_microusd"] == ledger_before["reserved_microusd"]
    calls = [entry["call"] for entry in clients["_native"].record]
    mutating = [
        call
        for call in calls
        if call
        in (
            "deploy_revision",
            "update_traffic",
            "resume_queue",
            "pause_queue",
            "create_object",
        )
    ]
    assert mutating == [
        "deploy_revision",
        "update_traffic",
        "resume_queue",
        "pause_queue",
        "create_object",
        "update_traffic",
        "resume_queue",
    ]
    assert TOKEN not in json.dumps(clients["_native"].record)


def test_native_transport_failure_during_readback_ends_unproven_without_resubmission(
    token, native_offset, tmp_path
):
    """A transport that drops after the deploy PATCH leaves the operation unproven
    under its own identity; apply asks for reconciliation and patches nothing again."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = scenario.dir / "release-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    original = cloud._run

    def dropped(method, name, query, payload):
        if name.endswith(":wait"):
            raise urllib.error.URLError("connection reset by peer")
        return original(method, name, query, payload)

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(
            clients,
            "apply",
            "--plan",
            plan_path,
            "--deadline-seconds",
            "0.05",
            "--output",
            result_path,
        )
        == 1
    )
    result = read(result_path)
    assert result["state"] == "unproven_requires_reconciliation"
    operation = result["operations"]["deploy_revision"]
    assert operation["state"] == "unproven"
    assert operation["resubmitted"] is False
    assert operation["last_state"] == "unknown"
    assert "URLError" in operation["last_error"]
    assert operation["operation"].startswith(SERVICE_API.rsplit("/services/", 1)[0])
    assert [
        (method, url) for method, url in cloud.mutations() if method == "PATCH"
    ] == [("PATCH", native.RUN_ENDPOINT + SERVICE_API + "?updateMask=template")]
    waits = {url for method, url in cloud.mutations() if url.endswith(":wait")}
    assert waits == {native.RUN_ENDPOINT + operation["operation"] + ":wait"}
    assert "route_traffic" not in result["operations"]
    assert cloud.service["traffic"] == scenario.state["service"]["traffic"]
    assert cloud.queue["state"] == "PAUSED"

    cloud._run = original
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 1
    )
    verified = read(verified_path)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["operation"] == operation["operation"]
    assert reconciled["state"] == "succeeded"
    assert {item["item"] for item in verified["drift"]} == {"traffic"}


# The release index over the native byte reader


def test_release_index_foreign_bucket_never_reaches_the_native_transport(
    token, tmp_path
):
    """The grammar refuses an unapproved bucket, so the owner credential is never
    spent reading an object the resource manifest does not cover."""
    scenario = Scenario(tmp_path)
    index, _references = release_index_fixture(scenario)
    index["source_binding"]["uri"] = index["source_binding"]["uri"].replace(
        release.BUCKET, "another-projects-bucket"
    )
    transport = FakeTransport([])
    clients = build(transport)
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index(index, byte_reader=clients["bytes"].read)
    assert transport.requests == []
    assert clients["_native"].record == []


def test_the_unbound_byte_reader_resolves_none_of_the_index_references(token, tmp_path):
    """The unbound reader stays deliberately narrow.

    ``bytes.read`` resolves only a reference that carries its own locator, and
    under ``42_staging_release_v3`` no index reference is one: the raw authority
    objects and the source binding are provenance the validator does not fetch,
    and every value it does fetch is either a bare name or a pinned evidence
    reference. So the unbound reader answers None for all fourteen even with
    every object sitting in the bucket, and the validator driven by it refuses.
    Resolving them is the bound reader's job, and
    ``ops/tests/test_release_index.py::test_native_reader_resolves_every_reference_kind_from_its_native_source``
    proves it does so for all fourteen against a transport that answers Cloud
    Build, the registry, the bucket and the tagged revision. This double answers
    only Cloud Run, Cloud Tasks and the bucket, so the bound reader is exercised
    here over the ten references the bucket alone can serve.
    """
    scenario = Scenario(tmp_path)
    index, references = release_index_fixture(scenario)
    cloud = FakeCloud(scenario.state)
    for reference, item in index["evidence_objects"].items():
        cloud.objects[item["uri"].split("/", 3)[3]] = {
            "generation": item["generation"],
            "raw": references[reference],
        }
    clients = build(cloud)
    unbound = clients["bytes"].read
    assert len(references) == 14
    assert sorted(references) == [
        "app_image",
        "app_tree",
        "asset:app.js",
        "authority:activation_ledger",
        "authority:deployment_binding",
        "commit",
        "contract:question_timeout",
        "deployment",
        "engine_image",
        "engine_tree",
        "ops_tree",
        "policy",
        "resource_manifest",
        "source_binding",
    ]
    assert {name: unbound(name) for name in sorted(references)} == dict.fromkeys(
        references
    )
    assert cloud.calls == [], "not one of them even reached the transport"
    with pytest.raises(ValueError, match="release_index_bytes_unavailable"):
        release.validate_release_index(index, byte_reader=unbound)
    # Narrow, not broken: the two kinds that do carry a locator still resolve,
    # and a bucket the resource manifest does not cover is still refused.
    pinned = index["evidence_objects"]["ops_tree"]
    assert (
        unbound(f"object:{pinned['uri']}#{pinned['generation']}")
        == references["ops_tree"]
    )
    assert unbound(f"object:{pinned['uri']}#{int(pinned['generation']) + 1}") is None
    with pytest.raises(ValueError, match="object_bucket_not_approved"):
        unbound("object:gs://another-projects-bucket/x/y.json#7")
    # The bound reader resolves what this double can answer: the eight pinned
    # evidence objects and the two digest named objects read live.
    origin = release.asset_origin(
        scenario.binding["canonical_service_audience"], scenario.tag
    )
    bound = clients["bytes"].reader(index, asset_origin=origin)
    storage_backed = [*sorted(index["evidence_objects"]), "policy", "deployment"]
    assert len(storage_backed) == 10
    assert {name: bound(name) for name in storage_backed} == {
        name: references[name] for name in storage_backed
    }


# The transport itself: no credential ever follows a redirect to another host


def _local_server(respond):
    """One real HTTP server on loopback; ``respond`` answers every request."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            status, headers, body = respond(self)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer
        do_PATCH = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_transport_refuses_a_cross_host_redirect_before_the_token_leaves(token):
    """A real server answers 302 with a Location on a real second host; the bearer
    token must not reach it, and the refusal must name the status and both hosts."""
    stolen = []

    def sink(handler):
        stolen.append(dict(handler.headers))
        return 200, {"Content-Type": "application/json"}, b'{"stolen":true}'

    foreign = _local_server(sink)
    elsewhere = f"http://127.0.0.1:{foreign.server_port}/steal"

    def redirect(_handler):
        return 302, {"Location": elsewhere}, b""

    origin = _local_server(redirect)
    try:
        with pytest.raises(native.RedirectRefused) as raised:
            native.urllib_transport(
                {
                    "method": "GET",
                    "url": f"http://127.0.0.1:{origin.server_port}/v2/services/x",
                    "headers": {
                        "Authorization": "Bearer " + TOKEN,
                        "Accept": "application/json",
                    },
                    "body": None,
                }
            )
        assert "redirect_refused: http 302" in str(raised.value)
        assert f"127.0.0.1:{foreign.server_port}" in str(raised.value)
        assert f"127.0.0.1:{origin.server_port}" in str(raised.value)
        assert TOKEN not in str(raised.value)
        assert stolen == []
    finally:
        origin.shutdown()
        origin.server_close()
        foreign.shutdown()
        foreign.server_close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_transport_follows_no_redirect_at_all_not_even_on_the_same_host(token, code):
    """A redirect on a Google API call is not accommodated. A same host redirect is
    refused too: 301, 302 and 303 would drop the POST body and turn the call into a
    GET, so a pause that never ran would read back as a success."""
    seen = []

    def respond(handler):
        seen.append((handler.command, handler.path))
        if handler.path.endswith(":pause"):
            return code, {"Location": "/queues/q"}, b""
        return 200, {"Content-Type": "application/json"}, b'{"state":"PAUSED"}'

    server = _local_server(respond)
    try:
        with pytest.raises(native.RedirectRefused) as raised:
            native.urllib_transport(
                {
                    "method": "POST",
                    "url": f"http://127.0.0.1:{server.server_port}/v2/queues/q:pause",
                    "headers": {
                        "Authorization": "Bearer " + TOKEN,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    "body": b"{}",
                }
            )
        assert f"redirect_refused: http {code}" in str(raised.value)
        assert TOKEN not in str(raised.value)
        assert seen == [("POST", "/v2/queues/q:pause")]
    finally:
        server.shutdown()
        server.server_close()


def test_a_two_hundred_that_is_not_json_refuses_instead_of_counting_as_success(token):
    """An error page served with a 200 is not a successful call."""

    def respond(_handler):
        return 200, {"Content-Type": "text/html"}, b"<html>proxy error</html>"

    server = _local_server(respond)
    try:
        clients = build(None)
        clients["_native"].transport = native.urllib_transport
        original = native.build_request

        def local(call, **kwargs):
            request = original(call, **kwargs)
            request["url"] = f"http://127.0.0.1:{server.server_port}/probe"
            return request

        native.build_request = local
        try:
            with pytest.raises(native.NativeError, match="response_not_json"):
                clients["run"].get_service(SERVICE_API)
        finally:
            native.build_request = original
        entry = clients["_native"].record[0]
        assert entry["status"] == 200
        assert entry["response"] == {"raw": "<html>proxy error</html>"}
    finally:
        server.shutdown()
        server.server_close()


def test_a_transport_result_without_a_status_refuses_by_name(token):
    clients = native.build("owner-gcloud", transport=lambda _request: {"body": b"{}"})
    with pytest.raises(native.NativeError, match="transport_response_invalid"):
        clients["run"].get_service(SERVICE_API)
    assert clients["_native"].record == []


# The endpoints, written as literals so a changed constant fails


def test_the_api_endpoints_are_the_literal_google_endpoints(token, tmp_path):
    assert native.RUN_ENDPOINT == "https://run.googleapis.com/v2/"
    assert native.TASKS_ENDPOINT == "https://cloudtasks.googleapis.com/v2/"
    assert native.STORAGE_ENDPOINT == "https://storage.googleapis.com/storage/v1/"
    assert native.UPLOAD_ENDPOINT == (
        "https://storage.googleapis.com/upload/storage/v1/"
    )
    service = (
        "projects/ogilvy-trends-v2/locations/us-central1/services/"
        "listening-post-staging"
    )
    queue = (
        "projects/ogilvy-trends-v2/locations/us-central1/queues/"
        "oi-general-question-staging"
    )
    assert native.build_request("get_service", name=service)["url"] == (
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/services/listening-post-staging"
    )
    assert native.build_request(
        "deploy_revision", service_name=service, template={"revision": "r"}
    )["url"] == (
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/services/listening-post-staging?updateMask=template"
    )
    assert native.build_request(
        "read_operation",
        name="projects/ogilvy-trends-v2/locations/us-central1/operations/op-1",
        timeout_seconds=30.0,
    )["url"] == (
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/operations/op-1:wait"
    )
    assert native.build_request("get_queue", name=queue)["url"] == (
        "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/queues/oi-general-question-staging"
    )
    assert native.build_request("pause_queue", name=queue)["url"] == (
        "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/queues/oi-general-question-staging:pause"
    )
    assert native.build_request("list_tasks", name=queue)["url"] == (
        "https://cloudtasks.googleapis.com/v2/projects/ogilvy-trends-v2/locations/"
        "us-central1/queues/oi-general-question-staging/tasks"
        "?responseView=BASIC&pageSize=1000"
    )
    assert native.build_request("object_metadata", name="a/b.json")["url"] == (
        "https://storage.googleapis.com/storage/v1/b/"
        "listening-post-staging-cache/o/a%2Fb.json"
    )
    assert native.build_request("object_media", name="a/b.json", generation="7")[
        "url"
    ] == (
        "https://storage.googleapis.com/storage/v1/b/"
        "listening-post-staging-cache/o/a%2Fb.json?alt=media&generation=7"
    )
    assert native.build_request(
        "create_object", name="a/b.json", raw=b"{}", if_generation_match=4
    )["url"] == (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        "listening-post-staging-cache/o?uploadType=media&name=a%2Fb.json"
        "&ifGenerationMatch=4"
    )


# The headers actually sent


EXPECTED_HEADERS = {
    "get_service": {"Authorization", "Accept"},
    "get_revision": {"Authorization", "Accept"},
    "get_queue": {"Authorization", "Accept"},
    "list_tasks": {"Authorization", "Accept"},
    "object_metadata": {"Authorization", "Accept"},
    "object_media": {"Authorization"},
    "read_operation": {"Authorization", "Accept", "Content-Type"},
    "pause_queue": {"Authorization", "Accept", "Content-Type"},
    "resume_queue": {"Authorization", "Accept", "Content-Type"},
    "create_object": {"Authorization", "Accept", "Content-Type"},
    "deploy_revision": {
        "Authorization",
        "Accept",
        "Content-Type",
        "X-Idempotency-Key",
    },
    "update_traffic": {
        "Authorization",
        "Accept",
        "Content-Type",
        "X-Idempotency-Key",
    },
}


class HeaderCloud(FakeCloud):
    """The fake cloud, keeping the headers of every request beside the call."""

    def __init__(self, state, **kwargs):
        super().__init__(state, **kwargs)
        self.headers = []

    def __call__(self, request):
        self.headers.append(dict(request["headers"]))
        return super().__call__(request)


def test_no_header_other_than_authorization_ever_carries_the_owner_token(
    token, native_offset, tmp_path
):
    """The record hiding the token is not enough: assert the header set that each
    call actually puts on the wire, so an echo of the token anywhere fails."""
    scenario = lifecycle_scenario(tmp_path)
    clients = build(None)
    cloud = HeaderCloud(scenario.state)
    clients["_native"].transport = cloud
    released(scenario, cloud, clients, native_offset)
    rollback_plan = scenario.dir / "rollback-plan.json"
    assert (
        run_native(
            clients,
            "rollback-plan",
            "--target-revision",
            SERVICE + "-q-old",
            "--authority",
            scenario.rollback_authority,
            "--output",
            rollback_plan,
        )
        == 0
    )
    assert (
        run_native(
            clients,
            "rollback-apply",
            "--plan",
            rollback_plan,
            "--output",
            scenario.dir / "rollback-result.json",
        )
        == 0
    )
    record = clients["_native"].record
    assert len(cloud.headers) == len(record)
    seen = set()
    for headers, entry in zip(cloud.headers, record, strict=True):
        call = entry["call"]
        seen.add(call)
        assert set(headers) == EXPECTED_HEADERS[call], (call, sorted(headers))
        assert headers["Authorization"] == "Bearer " + TOKEN
        carriers = [name for name, value in headers.items() if TOKEN in str(value)]
        assert carriers == ["Authorization"], (call, carriers)
    assert {"deploy_revision", "update_traffic", "create_object", "object_media"} <= seen


def test_the_idempotency_key_is_sent_on_the_wire(token, tmp_path):
    scenario = Scenario(tmp_path)
    template = release.expected_template(scenario.binding, scenario.tag)
    transport = FakeTransport(
        [
            (200, scenario.state["service"]),
            (200, {"name": OPERATION, "done": False}),
            (200, {"name": OPERATION, "done": False}),
            (200, {"name": SERVICE_API}),
        ]
    )
    clients = build(transport)
    clients["run"].deploy_revision(
        SERVICE_API,
        revision=scenario.binding["revision_name"],
        template=template,
        idempotency_key="k" * 64,
    )
    clients["run"].update_traffic(
        SERVICE_API,
        etag="etag-before",
        traffic=release.traffic_targets(scenario.binding, scenario.tag),
        idempotency_key="j" * 64,
    )
    clients["run"].get_service(SERVICE_API)
    assert "X-Idempotency-Key" not in transport.requests[0]["headers"]
    assert transport.requests[1]["headers"]["X-Idempotency-Key"] == "k" * 64
    assert transport.requests[2]["headers"]["X-Idempotency-Key"] == "j" * 64
    assert "X-Idempotency-Key" not in transport.requests[3]["headers"]
    assert clients["_native"].record[1]["idempotency_key"] == "k" * 64


# The approved evidence bucket, in the grammar and in the byte reader


def test_release_index_refuses_a_bucket_that_extends_the_approved_name(token, tmp_path):
    """A prefix extended bucket is a different bucket; only a separator keeps the
    two apart, so the check may not depend on one."""
    scenario = Scenario(tmp_path)
    index, _references = release_index_fixture(scenario)
    index["source_binding"]["uri"] = (
        f"gs://{release.BUCKET}-archive/source/snapshot.json"
    )
    transport = FakeTransport([])
    clients = build(transport)
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index(index, byte_reader=clients["bytes"].read)
    assert transport.requests == []


def test_release_index_refuses_a_uri_that_merely_contains_the_approved_bucket(
    token, tmp_path
):
    """The approved name appearing anywhere in the reference is not the approved
    bucket, so the check may not be a search."""
    scenario = Scenario(tmp_path)
    index, _references = release_index_fixture(scenario)
    index["raw_authority_objects"][0]["uri"] = (
        f"gs://another-projects-bucket/gs://{release.BUCKET}/authority/grant.json"
    )
    transport = FakeTransport([])
    clients = build(transport)
    with pytest.raises(ValueError, match="release_index_invalid"):
        release.validate_release_index(index, byte_reader=clients["bytes"].read)
    assert transport.requests == []


def test_release_index_refuses_an_object_name_holding_whitespace_or_a_hash(
    token, tmp_path
):
    """The grammar also fixes what a name may contain. A ``#`` is how a reference
    spells the generation, so a name carrying one names a different object once
    the reference is split again, and whitespace inside a name survives no round
    trip through a URL. Both are part of the original grammar and neither is
    reached by any other check, so the split alone would accept them."""
    scenario = Scenario(tmp_path)
    for name in (
        "source/snap shot.json",
        "source/snapshot.json#7",
        "source/snapshot.json\n",
        "\tsource/snapshot.json",
    ):
        uri = f"gs://{release.BUCKET}/{name}"
        assert release.approved_object_name(uri) is None, uri
        index, _references = release_index_fixture(scenario)
        index["source_binding"]["uri"] = uri
        transport = FakeTransport([])
        clients = build(transport)
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index(index, byte_reader=clients["bytes"].read)
        assert transport.requests == []
    assert (
        release.approved_object_name(f"gs://{release.BUCKET}/source/snapshot.json")
        == "source/snapshot.json"
    )


def test_release_index_refuses_a_reference_that_names_no_object(token, tmp_path):
    """The split can also leave nothing behind. A reference that stops at the
    bucket yields an empty name, which is the bucket itself and not an object,
    and a reference with no separator at all yields no name to split. The binding
    check asks only whether a name came back, so an empty one would satisfy it
    and reach a reader holding the owner credential."""
    scenario = Scenario(tmp_path)
    for uri in (f"gs://{release.BUCKET}/", f"gs://{release.BUCKET}", "gs://"):
        assert release.approved_object_name(uri) is None, uri
        index, _references = release_index_fixture(scenario)
        index["source_binding"]["uri"] = uri
        transport = FakeTransport([])
        clients = build(transport)
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index(index, byte_reader=clients["bytes"].read)
        assert transport.requests == []


def test_release_index_refuses_a_reference_whose_scheme_is_not_the_object_one(
    token, tmp_path
):
    """The split drops exactly the five characters of the object scheme before it
    reads the bucket, so any other five character opening puts the approved
    bucket name straight into the bucket position and the equality comparison
    agrees with it. The scheme test is therefore load bearing on its own and not
    a formality ahead of the bucket comparison."""
    scenario = Scenario(tmp_path)
    for uri in (
        "http:" + release.BUCKET + "/source/snapshot.json",
        "file:" + release.BUCKET + "/source/snapshot.json",
        "http://" + release.BUCKET + "/source/snapshot.json",
        release.BUCKET + "/source/snapshot.json",
    ):
        assert release.approved_object_name(uri) is None, uri
        index, _references = release_index_fixture(scenario)
        index["source_binding"]["uri"] = uri
        transport = FakeTransport([])
        clients = build(transport)
        with pytest.raises(ValueError, match="release_index_invalid"):
            release.validate_release_index(index, byte_reader=clients["bytes"].read)
        assert transport.requests == []


def test_the_byte_reader_refuses_an_object_outside_the_approved_bucket(token):
    """The second barrier: even handed a foreign reference directly, the reader
    spends no credential on it."""
    transport = FakeTransport([])
    clients = build(transport)
    for reference in (
        "object:gs://other-bucket/x/y.json#7",
        f"object:gs://{release.BUCKET}-archive/x/y.json#7",
        f"object:gs://another/gs://{release.BUCKET}/x/y.json#7",
    ):
        with pytest.raises(ValueError, match="object_bucket_not_approved"):
            clients["bytes"].read(reference)
    assert transport.requests == []


# Rollback ordering: the plan digest is recomputable, so the order is enforced


def reseal(path, plan):
    """Re-seal a plan the way anyone who can write one would."""
    plan.pop("idempotency_key", None)
    release._seal(plan)
    Path(path).write_text(json.dumps(plan), encoding="utf-8")
    return plan


def rolled_back_to_old(scenario, cloud, clients, offset, *, resume_first=False):
    """Release, optionally resume, then plan a rollback to the previous revision."""
    route_path = released(scenario, cloud, clients, offset)
    if resume_first:
        resume_plan = scenario.dir / "resume-plan.json"
        assert (
            run_native(
                clients,
                "plan",
                "--kind",
                "resume",
                "--mode",
                "empty",
                "--route",
                route_path,
                "--activation",
                scenario.activation_path,
                "--authority",
                scenario.authority_path,
                "--output",
                resume_plan,
            )
            == 0
        )
        assert (
            run_native(
                clients,
                "resume",
                "--plan",
                resume_plan,
                "--output",
                scenario.dir / "resume-result.json",
            )
            == 0
        )
        assert cloud.queue["state"] == "RUNNING"
    plan_path = scenario.dir / "rollback-plan.json"
    assert (
        run_native(
            clients,
            "rollback-plan",
            "--target-revision",
            SERVICE + "-q-old",
            "--authority",
            scenario.rollback_authority,
            "--output",
            plan_path,
        )
        == 0
    )
    return plan_path


def test_rollback_apply_refuses_a_resealed_plan_that_routes_before_activating(
    token, native_offset, tmp_path
):
    """Swapping the route ahead of the ledger write and re-sealing must change
    nothing: no traffic move, no ledger write, refused before any request."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    plan = read(plan_path)
    names = [change["operation"] for change in plan["changes"]]
    i, j = names.index("activate_binding"), names.index("route_traffic")
    plan["changes"][i], plan["changes"][j] = plan["changes"][j], plan["changes"][i]
    reseal(plan_path, plan)
    generation_before = cloud.objects[LEDGER]["generation"]
    mutations_before = len(cloud.mutations())

    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["state"] == "refused"
    assert result["error"] == "rollback_change_order_invalid"
    assert cloud.mutations()[mutations_before:] == []
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    assert cloud.objects[LEDGER]["generation"] == generation_before
    assert result["operations"] == {}


def test_rollback_apply_refuses_a_plan_whose_queue_pause_is_not_first(
    token, native_offset, tmp_path
):
    """The pause is not merely present somewhere; it is the first change or the
    plan is refused."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(
        scenario, cloud, clients, native_offset, resume_first=True
    )
    plan = read(plan_path)
    assert [change["operation"] for change in plan["changes"]] == [
        "pause_queue",
        "activate_binding",
        "route_traffic",
    ]
    plan["changes"].insert(1, plan["changes"].pop(0))
    reseal(plan_path, plan)
    mutations_before = len(cloud.mutations())

    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["state"] == "refused"
    assert result["error"] == "rollback_change_order_invalid"
    assert cloud.mutations()[mutations_before:] == []
    assert cloud.queue["state"] == "RUNNING"


def test_rollback_apply_refuses_a_plan_with_a_repeated_or_missing_change(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    original = read(plan_path)
    mutations_before = len(cloud.mutations())
    for changes in (
        [original["changes"][0]],
        [original["changes"][1]],
        [*original["changes"], original["changes"][1]],
        [original["changes"][0], original["changes"][0], original["changes"][1]],
    ):
        plan = copy.deepcopy(original)
        plan["changes"] = copy.deepcopy(changes)
        reseal(plan_path, plan)
        result_path = scenario.dir / "rollback-result.json"
        if result_path.exists():
            result_path.unlink()
        assert (
            run_native(
                clients, "rollback-apply", "--plan", plan_path, "--output", result_path
            )
            == 1
        )
        assert read(result_path)["error"] == "rollback_change_order_invalid"
    assert cloud.mutations()[mutations_before:] == []


def test_rollback_apply_refuses_to_route_when_the_ledger_lost_the_activation(
    token, native_offset, tmp_path
):
    """The precondition the release path already has: rollback checks the ledger
    against the binding before it moves any traffic."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    before_raw = cloud.objects[LEDGER]["raw"]
    upload = cloud._upload

    def overwritten(base, query, body):
        """Another writer puts the previous ledger back straight after the write."""
        response = upload(base, query, body)
        cloud._upload = upload
        name = query["name"]
        cloud.objects[name] = {
            "generation": str(cloud.next_generation),
            "raw": before_raw,
        }
        cloud.next_generation += 1
        return response

    cloud._upload = overwritten
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["error"] == "rollback_activation_unconfirmed"
    assert result["activation"]["state"] == "written"
    assert "route_traffic" not in result["operations"]
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    assert [
        (method, url.split("?")[0]) for method, url in cloud.mutations()
    ][-1] == ("POST", native.UPLOAD_ENDPOINT + f"b/{release.BUCKET}/o")

def test_rollback_apply_refuses_to_route_when_the_activation_reply_was_replayed(
    token, native_offset, tmp_path
):
    """The activation write is answered by a replayed or cached success carrying
    the generation the ledger already had, and the bytes never land. The receipt
    then records the activation as written and the generation it read back agrees
    with the one it stored, so neither the written clause nor the generation
    clause of the route precondition notices anything: only the digest clause
    sees that the ledger never moved. Without it rollback routes production
    traffic to the target while the ledger still binds the release deployment,
    which is the state the precondition exists to refuse."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    before = copy.deepcopy(cloud.objects[LEDGER])
    ledger_before = ledger_of(cloud)
    assert ledger_before["active_deployment_digest"] != read(plan_path)["binding"][
        "deployment_digest"
    ]

    def replayed(base, query, body):
        """A cached success for a write the store never took."""
        return _json(200, {"name": query["name"], "generation": before["generation"]})

    cloud._upload = replayed
    mutations_before = len(cloud.mutations())
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["error"] == "rollback_activation_unconfirmed"
    assert result["activation"]["state"] == "written"
    assert result["activation"]["generation"] == before["generation"]
    assert "route_traffic" not in result["operations"]
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    assert [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("updateMask", [""])[
            0
        ]
        for method, url in cloud.mutations()[mutations_before:]
        if method == "PATCH"
    ] == []
    assert cloud.objects[LEDGER] == before
    assert ledger_of(cloud) == ledger_before


def test_rollback_apply_refuses_to_route_when_the_ledger_moved_after_the_activation(
    token, native_offset, tmp_path
):
    """The activation lands, and another writer then appends a binding to the
    ledger without disturbing the active one. The digest clause of the route
    precondition is satisfied, so only the generation clause sees that the object
    the step wrote is no longer the object it is about to route against, and the
    requests and reservations this run classified may have moved with it."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    target_digest = read(plan_path)["binding"]["deployment_digest"]
    upload = cloud._upload

    def appended(base, query, body):
        """Another writer adds an unrelated binding straight after the write."""
        response = upload(base, query, body)
        cloud._upload = upload
        name = query["name"]
        value = json.loads(bytes(body).decode("utf-8"))
        value["bindings"]["c" * 64] = "d" * 64
        cloud.objects[name] = {
            "generation": str(cloud.next_generation),
            "raw": json.dumps(value).encode("utf-8"),
        }
        cloud.next_generation += 1
        return response

    cloud._upload = appended
    mutations_before = len(cloud.mutations())
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["error"] == "rollback_activation_unconfirmed"
    assert result["activation"]["state"] == "written"
    assert ledger_of(cloud)["active_deployment_digest"] == target_digest
    assert cloud.objects[LEDGER]["generation"] != result["activation"]["generation"]
    assert "route_traffic" not in result["operations"]
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    assert [
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("updateMask", [""])[
            0
        ]
        for method, url in cloud.mutations()[mutations_before:]
        if method == "PATCH"
    ] == []


def test_rollback_recovery_plan_names_the_route_when_the_traffic_write_drops(
    token, native_offset, tmp_path
):
    """The response to the traffic PATCH never arrives. The recovery plan must not
    tell the operator to prepare a forward activation as if nothing was routed."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "traffic":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-old"
    recovery = result["forward_recovery_plan"]
    assert recovery["route"]["target_revision"] == SERVICE + "-q-old"
    assert recovery["route"]["activation_generation"] == (
        result["activation"]["generation"]
    )
    assert result["operations"] == {}
    assert recovery["route"]["operation"] is None
    assert recovery["route"]["state"] == "unknown"
    assert "prepare a compatible forward activation" not in recovery["next"]
    assert "no route operation name survived the write" in recovery["next"]
    assert "route the target revision again" in recovery["next"]


# The rollback guarantees the branch left untested


def test_rollback_refuses_when_the_written_ledger_changed_a_request(
    token, native_offset, tmp_path
):
    """A preserved request comes back with a different deadline. The request count
    and the reservation are untouched, so only the canonical request bytes can
    catch it."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    upload = cloud._upload

    def rewritten(base, query, body):
        response = upload(base, query, body)
        cloud._upload = upload
        stored = json.loads(cloud.objects[query["name"]]["raw"].decode("utf-8"))
        row = stored["requests"][min(stored["requests"])]
        row["deadline_at"] = "2031-01-01T00:00:00Z"
        cloud.objects[query["name"]]["raw"] = json.dumps(stored).encode("utf-8")
        return response

    cloud._upload = rewritten
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["error"] == "requests_changed"
    stored = ledger_of(cloud)
    assert stored["reserved_microusd"] == len(stored["requests"]) * 100_000


def test_rollback_refuses_when_the_queue_is_running_at_the_end(
    token, native_offset, tmp_path
):
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(scenario, cloud, clients, native_offset)
    run = cloud._run

    def resumed_by_someone_else(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "traffic":
            cloud.queue["state"] = "RUNNING"
        return response

    cloud._run = resumed_by_someone_else
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    assert read(result_path)["error"] == "queue_not_paused"
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-old"


def test_rollback_refuses_when_the_ledger_moved_under_the_queue_pause(
    token, native_offset, tmp_path
):
    """The pause readback is not decoration: the ledger generation it reads back
    must still be the generation the plan was built on."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = rolled_back_to_old(
        scenario, cloud, clients, native_offset, resume_first=True
    )
    assert read(plan_path)["changes"][0]["operation"] == "pause_queue"
    tasks = cloud._tasks

    def moved(method, name):
        response = tasks(method, name)
        if name.endswith(":pause"):
            cloud._tasks = tasks
            cloud.objects[LEDGER]["generation"] = str(cloud.next_generation)
            cloud.next_generation += 1
        return response

    cloud._tasks = moved
    mutations_before = len(cloud.mutations())
    result_path = scenario.dir / "rollback-result.json"
    assert (
        run_native(
            clients, "rollback-apply", "--plan", plan_path, "--output", result_path
        )
        == 1
    )
    result = read(result_path)
    assert result["error"] == "before_state_changed"
    assert cloud.queue["state"] == "PAUSED"
    assert [
        (method, url.split("?")[0])
        for method, url in cloud.mutations()[mutations_before:]
    ] == [("POST", native.TASKS_ENDPOINT + QUEUE_API + ":pause")]
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"


# A dropped response to the write itself


def test_a_dropped_deploy_write_response_leaves_something_to_reconcile(
    token, native_offset, tmp_path
):
    """The server applied the PATCH and the reply never came back, so no operation
    name exists. The receipt must still name what verify reconciles against."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = scenario.dir / "release-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    result = read(result_path)
    assert result["state"] == "unproven_requires_reconciliation"
    operation = result["operations"]["deploy_revision"]
    assert operation["operation"] is None
    assert operation["state"] == "unproven"
    assert operation["resubmitted"] is False
    assert "URLError" in operation["last_error"]
    assert operation["reconcile_by"] == {
        "kind": "revision",
        "revision_name": SERVICE + "-q-new",
    }
    assert [m for m in cloud.mutations() if m[0] == "PATCH"] == [
        ("PATCH", native.RUN_ENDPOINT + SERVICE_API + "?updateMask=template")
    ]
    assert cloud.service["traffic"] == scenario.state["service"]["traffic"]

    cloud._run = run
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 1
    )
    verified = read(verified_path)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "succeeded"
    assert reconciled["reconciled_by"] == "revision"
    assert {item["item"] for item in verified["drift"]} == {"traffic"}


def test_a_dropped_deploy_write_that_never_landed_stays_unproven(
    token, native_offset, tmp_path
):
    """The same drop, but the server never applied it: verify finds no revision and
    reconciliation must not turn that into success."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = scenario.dir / "release-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    run = cloud._run

    def never_applied(method, name, query, payload):
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return run(method, name, query, payload)

    cloud._run = never_applied
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    cloud._run = run
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 1
    )
    verified = read(verified_path)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["error"] == "revision_unavailable"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}


def test_a_two_hundred_and_one_on_the_deploy_write_still_leaves_a_receipt(
    token, native_offset, tmp_path
):
    """Only 200 is accepted, deliberately: every call this adapter makes is
    documented to answer 200, and a 2xx that is not 200 means the API did
    something other than what was asked. The strictness no longer costs a
    receipt, because an unexpected status on a write is recorded as unproven
    with the effect to reconcile against, exactly like a lost reply."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = scenario.dir / "release-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    run = cloud._run

    def created(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            return {**response, "status": 201}
        return response

    cloud._run = created
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    result = read(result_path)
    assert result["state"] == "unproven_requires_reconciliation"
    operation = result["operations"]["deploy_revision"]
    assert operation["operation"] is None
    assert "http 201" in operation["last_error"]
    assert operation["reconcile_by"]["revision_name"] == SERVICE + "-q-new"

    cloud._run = run
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 1
    )
    verified = read(verified_path)
    assert verified["reconciled_operations"]["deploy_revision"]["state"] == "succeeded"


def planned(scenario, clients):
    """Plan a release over the native adapter and hand back the plan path."""
    plan_path = scenario.dir / "release-plan.json"
    assert (
        run_native(
            clients,
            "plan",
            "--activation",
            scenario.activation_path,
            "--authority",
            scenario.authority_path,
            "--output",
            plan_path,
        )
        == 0
    )
    return plan_path


def verified_after(scenario, clients, plan_path, result_path, *, code):
    """Run verify over a result the drop left unproven."""
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == code
    )
    return read(verified_path)


def test_a_dropped_deploy_write_whose_revision_is_tampered_stays_unproven(
    token, native_offset, tmp_path
):
    """The revision is present but it is not the one the plan bound: it runs as a
    different identity. Presence is therefore not proof, and settling on presence
    alone would report a deploy that never landed as done, which is the one
    outcome release lifecycle code may never produce. The reconcile must run the
    revision check and leave the record unproven.
    """
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = planned(scenario, clients)
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    cloud._run = run
    revision_name = SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    assert revision_name in cloud.revisions
    cloud.revisions[revision_name]["serviceAccount"] = (
        "intruder@another-project.iam.gserviceaccount.com"
    )
    mutations_before = len(cloud.mutations())

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == "revision"
    assert reconciled["error"] == "revision_mismatch"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}
    assert cloud.mutations()[mutations_before:] == []


def test_a_dropped_deploy_write_whose_revision_is_not_ready_stays_unproven(
    token, native_offset, tmp_path
):
    """The same shape one step further in: the revision exists and matches the
    binding, but it never reached Ready. A record that settles on the name alone
    would call a revision that cannot serve a landed deploy."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = planned(scenario, clients)
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    cloud._run = run
    revision_name = SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    cloud.revisions[revision_name]["conditions"] = [
        {"type": "Ready", "state": "CONDITION_PENDING"}
    ]

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["error"] == "revision_not_ready"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}


def dropped_traffic_write(scenario, cloud, clients, *, applied):
    """Apply a release whose traffic PATCH loses its reply, landing or not."""
    plan_path = planned(scenario, clients)
    run = cloud._run

    def dropped(method, name, query, payload):
        if method == "PATCH" and query.get("updateMask") == "traffic":
            if applied:
                run(method, name, query, payload)
            raise urllib.error.URLError("connection reset by peer")
        return run(method, name, query, payload)

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    cloud._run = run
    result = read(result_path)
    assert result["state"] == "unproven_requires_reconciliation"
    operation = result["operations"]["route_traffic"]
    assert operation["operation"] is None
    assert operation["state"] == "unproven"
    assert operation["reconcile_by"] == {
        "kind": "traffic",
        "revision_name": SERVICE + "-q-new",
    }
    return plan_path, result_path


def test_a_dropped_traffic_write_that_never_routed_stays_unproven(
    token, native_offset, tmp_path
):
    """The traffic branch of the same reconcile. The service is present, so a
    reconcile that settles on presence would call the release routed while every
    request still reaches the previous revision."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_traffic_write(
        scenario, cloud, clients, applied=False
    )
    assert cloud.service["traffic"] == scenario.state["service"]["traffic"]

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["route_traffic"]
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == "traffic"
    assert reconciled["error"] == "route_not_terminal"
    assert "operation:route_traffic" in {item["item"] for item in verified["drift"]}


def test_a_dropped_traffic_write_that_routed_settles_as_succeeded(
    token, native_offset, tmp_path
):
    """The write did land before the reply was lost, so reading the effect back
    settles it. Without this the traffic branch could refuse everything and still
    look correct to the case above."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_traffic_write(
        scenario, cloud, clients, applied=True
    )
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"

    verified = verified_after(scenario, clients, plan_path, result_path, code=0)
    reconciled = verified["reconciled_operations"]["route_traffic"]
    assert reconciled["state"] == "succeeded"
    assert reconciled["reconciled_by"] == "traffic"
    assert verified["drift"] == []


def test_a_reconcile_whose_own_read_drops_records_the_failure_and_stays_unproven(
    token, native_offset, tmp_path
):
    """The reconcile reads the effect back over the same transport that already
    lost one reply, so its own read can lose one too. That must leave a receipt
    naming the failed read rather than propagating out of verify, and the record
    must still be unproven."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path = planned(scenario, clients)
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    revision_name = SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    reads = {"count": 0}

    def read_drops_once(method, name, query, payload):
        if method == "GET" and name == revision_name and reads["count"] == 0:
            reads["count"] += 1
            raise urllib.error.URLError("connection reset by peer")
        return run(method, name, query, payload)

    cloud._run = read_drops_once
    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == "revision"
    assert reconciled["error"].startswith("reconcile_read_failed: ")
    assert "URLError" in reconciled["error"]
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}


def dropped_deploy_write(scenario, cloud, clients):
    """Apply a release whose deploy PATCH lands and then loses its reply."""
    plan_path = planned(scenario, clients)
    run = cloud._run

    def dropped(method, name, query, payload):
        response = run(method, name, query, payload)
        if method == "PATCH" and query.get("updateMask") == "template":
            raise urllib.error.URLError("connection reset by peer")
        return response

    cloud._run = dropped
    result_path = scenario.dir / "release-result.json"
    assert (
        run_native(clients, "apply", "--plan", plan_path, "--output", result_path) == 1
    )
    cloud._run = run
    assert SERVICE_API + "/revisions/" + SERVICE + "-q-new" in cloud.revisions
    return plan_path, result_path


def with_reconcile_by(result_path, value, *, absent=False):
    """Put a different reconcile block on the saved receipt. Verify binds the
    result to the plan digest and to nothing else, so a receipt carrying a block
    this build does not read is exactly what reaches the reconcile after an
    upgrade, a downgrade or a hand edit."""
    result = read(result_path)
    operation = result["operations"]["deploy_revision"]
    if absent:
        operation.pop("reconcile_by")
    else:
        operation["reconcile_by"] = value
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    return result_path


GHOST = SERVICE + "-q-ghost"


@pytest.mark.parametrize(
    "value,absent,reconciled_by",
    [
        ({"kind": "operation", "revision_name": GHOST}, False, "operation"),
        (None, True, None),
    ],
    ids=["unrecognised_kind", "no_reconcile_block"],
)
def test_a_reconcile_with_no_target_it_reads_stays_unproven(
    token, native_offset, tmp_path, value, absent, reconciled_by
):
    """The third branch of the same reconcile. A record whose reconcile block
    names a kind this build does not read, or carries no block at all, has had no
    effect read back at all, so there is nothing that could settle it. Falling
    through to succeeded here would report a deploy that never landed as done
    having made no native call whatsoever, which is the outcome the other two
    branches exist to prevent."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_deploy_write(scenario, cloud, clients)
    with_reconcile_by(result_path, value, absent=absent)
    calls_before = len(cloud.calls)

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == reconciled_by
    assert reconciled["error"] == "reconcile_target_unknown"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}
    assert [
        call["url"] for call in cloud.calls[calls_before:] if GHOST in call["url"]
    ] == []


@pytest.mark.parametrize(
    "value",
    [
        ["revision", SERVICE + "-q-new"],
        "revision",
        {"kind": "revision"},
        {"kind": "revision", "revision_name": None},
    ],
    ids=["list", "string", "no_revision_name", "null_revision_name"],
)
def test_a_reconcile_whose_target_block_is_malformed_refuses_by_name(
    token, native_offset, tmp_path, value
):
    """A reconcile block that is not the shape this code reads is a malformed
    audit input, not a lost reply. It must be refused by its own name rather than
    filed on the receipt as a failed transport read, which is what an operator
    would otherwise be told to chase, and rather than escaping the reconcile as a
    raw attribute error on the record the reconcile is there to write."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_deploy_write(scenario, cloud, clients)
    with_reconcile_by(result_path, value)
    calls_before = len(cloud.calls)

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["error"] == "reconcile_target_malformed"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}
    assert [
        call["url"]
        for call in cloud.calls[calls_before:]
        if call["method"] == "GET" and "/revisions/" in call["url"]
    ] == [
        native.RUN_ENDPOINT + SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    ]


def test_a_reconcile_whose_service_read_is_unavailable_stays_unproven(
    token, native_offset, tmp_path
):
    """The traffic branch reads the service before it reads the route out of it.
    A service the transport answers as absent proves nothing about where traffic
    went, so the branch must refuse by name rather than carry a null into the
    route check."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_traffic_write(
        scenario, cloud, clients, applied=True
    )
    assert cloud.service["trafficStatuses"][0]["revision"] == SERVICE + "-q-new"
    run = cloud._run
    reads = {"count": 0}

    def service_gone_once(method, name, query, payload):
        if method == "GET" and name == SERVICE_API and reads["count"] == 0:
            reads["count"] += 1
            return _json(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
        return run(method, name, query, payload)

    cloud._run = service_gone_once
    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["route_traffic"]
    assert reads["count"] == 1
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == "traffic"
    assert reconciled["error"] == "service_unavailable"
    assert "operation:route_traffic" in {item["item"] for item in verified["drift"]}


def test_a_reconcile_whose_revision_is_malformed_refuses_rather_than_escaping(
    token, native_offset, tmp_path
):
    """The revision comes back, but not as a revision: the readback is a shape
    the check cannot walk. verify_revision owns that case and turns it into a
    mismatch refusal, so the reconcile records a named refusal on an unproven
    record instead of letting a raw lookup error out of verify."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_deploy_write(scenario, cloud, clients)
    revision_name = SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    cloud.revisions[revision_name]["scaling"] = None

    verified = verified_after(scenario, clients, plan_path, result_path, code=1)
    reconciled = verified["reconciled_operations"]["deploy_revision"]
    assert reconciled["state"] == "unproven"
    assert reconciled["reconciled_by"] == "revision"
    assert reconciled["error"] == "revision_mismatch"
    assert "operation:deploy_revision" in {item["item"] for item in verified["drift"]}


def test_a_reconcile_read_that_raises_a_value_error_is_not_filed_as_a_lost_reply(
    token, native_offset, tmp_path, capsys
):
    """A value error out of the readback is a refusal by name or a programming
    error; it is never the dropped reply the read failure clause exists for.
    Filing it as one would put a code on the receipt that names the transport for
    a fault that is not the transport's, so it leaves the reconcile untouched and
    no verify receipt is written at all."""
    scenario = lifecycle_scenario(tmp_path)
    cloud = FakeCloud(scenario.state)
    clients = build(cloud)
    plan_path, result_path = dropped_deploy_write(scenario, cloud, clients)
    revision_name = SERVICE_API + "/revisions/" + SERVICE + "-q-new"
    run = cloud._run
    reads = {"count": 0}

    def value_error_on_the_first_readback(method, name, query, payload):
        """Only the reconcile's own read raises. Every later read goes through,
        so if the reconcile filed this away and carried on it would reach the end
        of verify and write a receipt, which is the outcome under test."""
        if method == "GET" and name == revision_name and reads["count"] == 0:
            reads["count"] += 1
            raise ValueError("reconcile_probe_not_a_refusal")
        return run(method, name, query, payload)

    cloud._run = value_error_on_the_first_readback
    verified_path = scenario.dir / "release-verified.json"
    assert (
        run_native(
            clients,
            "verify",
            "--plan",
            plan_path,
            "--result",
            result_path,
            "--output",
            verified_path,
        )
        == 1
    )
    assert reads["count"] == 1
    assert not Path(verified_path).exists()
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["error"] == "reconcile_probe_not_a_refusal"
