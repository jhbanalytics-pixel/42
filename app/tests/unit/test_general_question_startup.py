import hashlib
import importlib
import io
import json
import logging
import os
import socket
import traceback
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient
from urllib3.response import HTTPResponse

from tests.unit.test_question_worker_bundle import canonical, make_bundle


def module():
    return importlib.import_module("src.api.general_question_startup")


def build():
    from src.api import main

    return module().build_general_question_routes(
        try_acquire=main._try_acquire_generation_capacity,
        release=main._release_generation_capacity,
    )


def setup(tmp_path, monkeypatch, failure=None):
    from src.api import main

    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    with main._generation_capacity_lock:
        main._generation_active = 0
    root, stamp, digest = make_bundle(tmp_path)
    identity = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    env = {
        "DEPLOYMENT_PROFILE": "open-intelligence-staging",
        "K_SERVICE": "listening-post-staging",
        "K_REVISION": "listening-post-staging-startup",
        "SOURCE_SHA": "1" * 40,
        "GCP_PROJECT": "ogilvy-trends-v2",
        "BQ_DATASET": "trends_v2_staging",
        "CACHE_BUCKET": "listening-post-staging-cache",
        "CACHE_PREFIX": "open-intelligence/v2/staging/",
        "APPLICATION_SOURCE": "open-intelligence-staging",
        "GENERAL_QUESTION_WORKER_URL": "https://worker---listening-post-staging-fibxg5ynpq-uc.a.run.app/internal/general-question/execute",
    }
    binding = {
        "contract_version": "general_question_deployment_v1",
        "project": "ogilvy-trends-v2",
        "region": "us-central1",
        "service_name": env["K_SERVICE"],
        "revision_name": env["K_REVISION"],
        "service_account_email": identity,
        "lp_commit": "1" * 40,
        "engine_bundle_digest": digest,
        "canonical_service_audience": "https://listening-post-staging-fibxg5ynpq-uc.a.run.app",
        "sdk_version": "2.20.0",
        "image_digest": "a" * 64,
        "worker_path": "/internal/general-question/execute",
        "policy_digest": "b" * 64,
    }
    if failure == "revision":
        binding["revision_name"] = "listening-post-staging-other"
    binding["deployment_digest"] = hashlib.sha256(canonical(binding)).hexdigest()
    env["GENERAL_QUESTION_DEPLOYMENT_DIGEST"] = binding["deployment_digest"]
    # The staging environment above is the whole environment. A vendor secret
    # inherited from the shell running the suite is not part of it; a test that
    # wants one sets it after setup, as the refusal tests below do.
    for key in list(os.environ):
        if key.endswith(("_API_KEY", "_ACCESS_TOKEN", "_SECRET")):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module(), "_ENGINE_ROOT", root)
    monkeypatch.setattr(module(), "_STAMP_PATH", stamp)
    interpreter = tmp_path / "worker-python"
    interpreter.write_text("synthetic")
    monkeypatch.setattr(module(), "_INTERPRETER", interpreter)
    if failure == "stamp":
        stamp.write_bytes(b"{}")
    if failure == "source":
        monkeypatch.setenv("SOURCE_SHA", "2" * 40)
    if failure == "binding_digest":
        binding["image_digest"] = "c" * 64
    original_connect = socket.socket.connect

    def connect_local_pipe(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        pytest.fail("external network attempted")

    monkeypatch.setattr(socket.socket, "connect", connect_local_pipe)
    calls = []

    def send(session, request, **kwargs):
        calls.append(request.url)
        parsed = urlparse(request.url)
        assert session.trust_env is False
        assert kwargs["timeout"] <= 5
        response = requests.Response()
        response.request = request
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        if parsed.hostname == "metadata.google.internal":
            response.headers["Metadata-Flavor"] = "Google"
            if failure == "metadata_redirect":
                response.status_code = 302
                response.headers["Location"] = "http://example.invalid/"
                data = {}
            elif parsed.path.endswith("project-id"):
                response.headers["Content-Type"] = "text/plain"
                response._content = (
                    b"foreign" if failure == "project" else b"ogilvy-trends-v2"
                )
                return response
            elif parsed.path.endswith("/token"):
                data = {
                    "access_token": "synthetic-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            else:
                data = {
                    "email": "foreign@example.invalid"
                    if failure == "principal"
                    else identity,
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                }
        else:
            assert parsed.hostname == "storage.googleapis.com"
            if failure == "missing":
                response.status_code = 404
                data = {"error": {"code": 404, "message": "synthetic missing"}}
            elif failure == "storage_redirect":
                response.status_code = 302
                response.headers["Location"] = "https://example.invalid/"
                data = {}
            elif parse_qs(parsed.query).get("alt") == ["media"]:
                raw = canonical(binding)
                response.headers["x-goog-generation"] = "7"
                response._content = raw
                response.raw = HTTPResponse(
                    body=io.BytesIO(raw),
                    preload_content=False,
                    headers=response.headers,
                )
                return response
            else:
                data = {
                    "generation": "7",
                    "size": str(len(canonical(binding))),
                    "bucket": "listening-post-staging-cache",
                }
        response._content = json.dumps(data).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return binding, calls


def test_actual_startup_factory_builds_routes_worker_queue_and_shared_capacity(
    tmp_path, monkeypatch
):
    from src.api.general_question_routes import GeneralQuestionRoutes
    from src.api.question_worker_controller import QuestionWorker

    binding, calls = setup(tmp_path, monkeypatch)
    first = build()
    second = build()
    assert isinstance(first, GeneralQuestionRoutes)
    assert isinstance(first.worker, QuestionWorker)
    assert first.policy_digest == binding["policy_digest"]
    assert first.queue.deployment_digest == binding["deployment_digest"]
    assert first.worker.try_acquire() is True
    try:
        assert second.worker.try_acquire() is False
    finally:
        first.worker.release()
    assert calls


@pytest.mark.parametrize(
    "failure",
    [
        "revision",
        "stamp",
        "source",
        "binding_digest",
        "missing",
        "principal",
        "project",
        "metadata_redirect",
        "storage_redirect",
    ],
)
def test_invalid_startup_binding_refuses(tmp_path, monkeypatch, failure):
    _, calls = setup(tmp_path, monkeypatch, failure)
    with pytest.raises(ValueError):
        build()
    assert not any("example.invalid" in call for call in calls)


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "STORAGE_EMULATOR_HOST",
        "GCE_METADATA_HOST",
        "GENERAL_QUESTION_WORKER_URL",
    ],
)
def test_unsafe_environment_refuses_before_requests(tmp_path, monkeypatch, name):
    _, calls = setup(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "unsafe")
    with pytest.raises(ValueError):
        build()
    assert calls == []


def test_main_startup_failure_keeps_normal_lp_available(monkeypatch, caplog):
    from src.api import main

    monkeypatch.delenv("GENERAL_QUESTION_DEPLOYMENT_DIGEST", raising=False)
    monkeypatch.setenv("K_REVISION", "listening-post-staging-safe")
    monkeypatch.setenv("SOURCE_SHA", "1" * 40)
    with TestClient(main.app) as client:
        assert client.get("/").status_code == 200
        assert getattr(main.app.state, "general_question_routes", None) is None
    assert "component=general_question_startup" in caplog.text
    assert "code=" in caplog.text
    assert "synthetic-token" not in caplog.text


def test_main_startup_installs_actual_verified_routes(tmp_path, monkeypatch):
    from src.api import main
    from src.api.general_question_routes import GeneralQuestionRoutes

    binding, calls = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(main.app.state, "general_question_routes", None, raising=False)
    with TestClient(main.app) as client:
        assert isinstance(main.app.state.general_question_routes, GeneralQuestionRoutes)
        assert (
            main.app.state.general_question_routes.deployment_digest
            == binding["deployment_digest"]
        )
        assert client.get("/").status_code == 200
    assert calls


def test_worker_cannot_bypass_existing_generation_capacity(tmp_path, monkeypatch):
    from src.api import main

    setup(tmp_path, monkeypatch)
    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    with main._generation_capacity_lock:
        main._generation_active = 0
    service = build()
    assert main._try_acquire_generation_capacity() is True
    acquired = False
    try:
        acquired = service.worker.try_acquire()
        assert acquired is False
    finally:
        if acquired:
            service.worker.release()
        main._release_generation_capacity()


def test_worker_failure_releases_the_existing_generation_slot(tmp_path, monkeypatch):
    import asyncio
    import time
    from src.api import main
    from tests.unit.test_question_worker_result import EXPECTED

    setup(tmp_path, monkeypatch)
    service = build()

    async def fail(*args, **kwargs):
        assert main._generation_active == 1
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr("src.api.question_worker_controller.invoke_engine", fail)
    with pytest.raises(RuntimeError, match="synthetic worker failure"):
        asyncio.run(
            service.worker.run("execute", EXPECTED, deadline=time.monotonic() + 5)
        )
    assert main._generation_active == 0


def test_startup_refusal_keeps_its_cause_and_still_says_nothing_more(
    tmp_path, monkeypatch, caplog
):
    """A wrapped startup refusal keeps the reason an operator can act on.

    The code the caller sees stays the generic one, and the log line stays the
    code alone, but the exception that caused the refusal remains attached. A
    refusal raised `from None` would drop it and leave a failed startup with no
    recoverable explanation, so the chain is pinned here.
    """
    from src.api import main

    setup(tmp_path, monkeypatch)
    original = RuntimeError("synthetic contract failure")

    def refuse(**kwargs):
        raise original

    monkeypatch.setattr(
        module().deployment_contract, "validate_runtime_contract", refuse
    )

    with pytest.raises(module().QuestionStartupError) as raised:
        build()
    assert str(raised.value) == "question_startup_unavailable"
    assert raised.value.__cause__ is original
    assert "synthetic contract failure" in "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )

    monkeypatch.setattr(main.app.state, "general_question_routes", None, raising=False)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        module().configure_general_question_startup(
            main.app,
            try_acquire=main._try_acquire_generation_capacity,
            release=main._release_generation_capacity,
        )
    assert main.app.state.general_question_routes is None
    assert (
        main.app.state.general_question_startup_code == "question_startup_unavailable"
    )
    assert "component=general_question_startup" in caplog.text
    assert "code=question_startup_unavailable" in caplog.text
    assert "synthetic contract failure" not in caplog.text
    assert "synthetic-token" not in caplog.text


def test_setup_does_not_inherit_an_ambient_vendor_secret(tmp_path, monkeypatch):
    """The staging environment setup builds is the whole environment.

    A developer shell or a sandbox can carry a vendor token such as
    CLOUDSDK_AUTH_ACCESS_TOKEN. The deployment contract rightly refuses one, so
    an inherited token made every startup test fail for a reason no test chose.
    """
    from src.api.general_question_routes import GeneralQuestionRoutes

    monkeypatch.setenv("AMBIENT_VENDOR_ACCESS_TOKEN", "ambient")
    monkeypatch.setenv("AMBIENT_VENDOR_API_KEY", "ambient")
    monkeypatch.setenv("AMBIENT_VENDOR_SECRET", "ambient")
    setup(tmp_path, monkeypatch)
    assert isinstance(build(), GeneralQuestionRoutes)


@pytest.mark.parametrize(
    "name",
    [
        "SYNTHETIC_VENDOR_ACCESS_TOKEN",
        "SYNTHETIC_VENDOR_API_KEY",
        "SYNTHETIC_VENDOR_SECRET",
    ],
)
def test_vendor_secret_set_after_setup_still_refuses_startup(
    tmp_path, monkeypatch, name
):
    """Clearing ambient secrets in setup never hides the contract's refusal."""
    from src.api.deployment_contract import DeploymentContractError

    setup(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "synthetic")
    with pytest.raises(module().QuestionStartupError) as raised:
        build()
    assert isinstance(raised.value.__cause__, DeploymentContractError)
    assert name in str(raised.value.__cause__)
