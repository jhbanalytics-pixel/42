import asyncio
import hashlib
import importlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.question_worker_controller import QuestionWorker
from src.api.question_worker_process import EngineReply
from src.api.question_worker_process import WorkerProcessError
from tests.unit.test_question_worker_controller import create_worker
from tests.unit.test_question_worker_result import EXPECTED
from tests.unit.test_question_worker_result import HASHES, receipt
from tests.unit.test_question_worker_deadline import raw_request


def module():
    return importlib.import_module("src.api.general_question_routes")


@pytest.mark.parametrize("operation", ["admission", "status"])
def test_invalid_input_is_logged_as_expected_refusal(caplog, operation):
    from fastapi import HTTPException

    service = module().GeneralQuestionRoutes(
        None,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            service.start({"message": "", "history": [], "market": "za"})
            if operation == "admission"
            else service.status("bad-id")
        )
    assert raised.value.status_code == 400
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"code":')
    ]
    assert records[-1]["code"] == "request_invalid"
    assert records[-1]["request_id"] is None


def test_authenticated_request_read_failure_retains_task_uuid_and_typed_code(
    monkeypatch, caplog
):
    from src.api.question_worker_result import ResultVerificationError

    monkeypatch.setattr(
        module(), "verify_question_worker_authorization", lambda *args, **kwargs: {}
    )

    def unavailable(*args, **kwargs):
        raise ResultVerificationError("result_store_unavailable")

    monkeypatch.setattr(module(), "read_request_bytes", unavailable)

    class Request:
        headers = SimpleNamespace(getlist=lambda name: ["synthetic"])

        async def stream(self):
            yield json.dumps(EXPECTED).encode()

    service = module().GeneralQuestionRoutes(
        SimpleNamespace(storage_client=None),
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    with pytest.raises(ResultVerificationError):
        asyncio.run(service.execute(Request()))
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"code":')
    ]
    assert records[-1]["request_id"] == EXPECTED["request_id"]
    assert records[-1]["operation"] == "request_read"
    assert records[-1]["code"] == "result_store_unavailable"
    assert [(row["operation"], row["state"]) for row in records[:-1]] == [
        ("authentication", "started"),
        ("request_read", "started"),
    ]


def test_parent_log_sink_failure_does_not_change_refusal(monkeypatch):
    from src.api import question_worker_process as process
    from fastapi import HTTPException

    class Worker:
        async def run(self, *args, **kwargs):
            raise WorkerProcessError("worker_launch_failed")

    def broken_log(*args, **kwargs):
        raise OSError("private-log-sink-secret")

    monkeypatch.setattr(process._DIAGNOSTIC_LOGGER, "log", broken_log)
    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service))
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            module().start_route(
                request, {"message": "Question", "history": [], "market": "za"}
            )
        )
    assert raised.value.status_code == 503
    assert raised.value.detail == "General question admission is unavailable"


@pytest.mark.parametrize("operation", ["admission", "status"])
@pytest.mark.parametrize(
    "error",
    [
        WorkerProcessError("worker_launch_failed"),
        RuntimeError("private-provider-secret"),
    ],
)
def test_parent_failures_log_safe_known_uuid_without_changing_public_response(
    monkeypatch, caplog, operation, error
):
    class Worker:
        async def run(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(module(), "uuid4", lambda: UUID(EXPECTED["request_id"]))
    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service))
    )

    async def run():
        if operation == "admission":
            return await module().start_route(
                request,
                {"message": "private-question-secret", "history": [], "market": "za"},
            )
        return await module().status_route(
            request, "chat_" + EXPECTED["request_id"].replace("-", "")
        )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        asyncio.run(run())
    assert raised.value.status_code == 503
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"code":')
    ]
    failed = [row for row in records if row["state"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["request_id"] == EXPECTED["request_id"]
    assert failed[0]["operation"] == operation
    assert failed[0]["code"] == (
        "worker_launch_failed"
        if isinstance(error, WorkerProcessError)
        else "worker_failed"
    )
    assert "secret" not in caplog.text


def test_auth_failure_never_attributes_the_unread_forged_body_uuid(caplog):
    class Request:
        headers = SimpleNamespace(getlist=lambda name: [])

        async def stream(self):
            pytest.fail("unauthenticated body was read")
            yield b""

    service = module().GeneralQuestionRoutes(
        None,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    with pytest.raises(Exception):
        asyncio.run(service.execute(Request()))
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"code":')
    ]
    assert records[-1]["request_id"] is None
    assert records[-1]["operation"] == "authentication"
    assert records[-1]["code"] == "worker_oidc_invalid"


@pytest.mark.parametrize("references", [
    {}, {"parent_request_id": None, "thread_anchor_request_id": None},
    {"parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"},
    {"parent_request_id": "ef06dcc1-79f5-490d-847f-868866d3ce83", "thread_anchor_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"},
])
def test_admission_uses_actual_controller_and_preserves_original_history(
    monkeypatch, tmp_path, references
):
    worker, _store, capacity, _captured = create_worker(tmp_path, operation="admit")
    observed = []

    async def controlled_process(root, interpreter, operation, payload, **kwargs):
        observed.append((operation, payload))
        return EngineReply(
            {
                "job_id": "chat_" + EXPECTED["request_id"].replace("-", ""),
                "invocation": EXPECTED,
                "deadline_at": "2099-01-01T00:00:00Z",
            },
            0,
            "0" * 64,
        )

    monkeypatch.setattr(
        "src.api.question_worker_controller.invoke_engine", controlled_process
    )
    monkeypatch.setattr(module(), "uuid4", lambda: UUID(EXPECTED["request_id"]))
    queued = []
    queue = SimpleNamespace(
        enqueue=lambda invocation, **kwargs: (
            queued.append(invocation) or {"state": "ambiguous"}
        )
    )
    service = module().GeneralQuestionRoutes(
        worker,
        queue,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    history = [{"role": "user", "text": "earlier"}] * 40
    question = "What is changing for Gen Z in South Africa?"
    result = asyncio.run(
        service.start({"message": question, "history": history, "market": "za", **references})
    )
    assert result == {"job_id": "chat_00000000000000000000000000000001"}
    assert observed[0][0] == "admit"
    assert observed[0][1]["transport"]["message"] == question
    assert observed[0][1]["transport"]["history"] == history
    assert observed[0][1]["scope"]["client_scope_id"] == "ogilvy_default"
    assert observed[0][1]["scope"]["audience_lens_ids"] == []
    payload = observed[0][1]
    if references.get("parent_request_id") is not None:
        assert payload["contract_version"] == "general_question_admission_v2"
        assert payload["parent_request_id"] == references["parent_request_id"]
        assert payload["thread_anchor_request_id"] == references.get("thread_anchor_request_id")
    else:
        assert payload["contract_version"] == "general_question_admission_v1"
        assert "parent_request_id" not in payload
        assert "thread_anchor_request_id" not in payload
    assert queued == [EXPECTED]
    assert not capacity.locked()
    assert isinstance(worker, QuestionWorker)


@pytest.mark.parametrize("references", [
    {"thread_anchor_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"},
    {"parent_request_id": "AD30EFED-DD99-49DA-8113-3943E289FBA0"},
    {"parent_request_id": "https://example.test/parent"},
    {"parent_context_ref": {}}, {"scope": {"client_scope_id": "other"}},
])
def test_parent_fields_refuse_before_worker_execution(references):
    from fastapi import HTTPException
    service = module().GeneralQuestionRoutes(
        None, None, policy_digest=EXPECTED["policy_digest"], deployment_digest=EXPECTED["deployment_digest"]
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.start({"message": "Follow up", "history": [], "market": "za", **references}))
    assert error.value.status_code == 400


def test_public_chat_parent_fields_are_strict_strings_and_extra_authority_refuses():
    from pydantic import ValidationError
    from src.api.main import ChatRequest
    base = {"message": "Follow up", "history": [], "market": "za"}
    refs = {"parent_request_id": "ef06dcc1-79f5-490d-847f-868866d3ce83", "thread_anchor_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"}
    assert ChatRequest.model_validate(base | refs).model_dump(exclude_none=True) == base | refs
    assert ChatRequest.model_validate(base).model_dump(exclude_none=True) == base
    for invalid in [
        {"parent_request_id": refs["parent_request_id"].encode()},
        {"parent_request_id": 1}, {"parent_request_id": {}},
        {"parent_context_ref": {}}, {"client_scope_id": "other"},
    ]:
        with pytest.raises(ValidationError):
            ChatRequest.model_validate(base | invalid)


def test_http_chat_keeps_gate_and_exact_legacy_or_parent_wire(monkeypatch):
    main = importlib.import_module("src.api.main")
    monkeypatch.setenv("UI_PASSCODE", "synthetic-test-passcode")
    monkeypatch.setattr(main, "_check_rate_limit", lambda request: None)
    observed = []
    class Worker:
        async def run(self, operation, payload, **kwargs):
            observed.append(payload)
            bound = {key: payload[key] for key in ("request_id", "policy_digest", "deployment_digest")}
            bound.update(contract_version="general_cultural_question_v1", request_digest="a" * 64, intake_digest="b" * 64)
            return SimpleNamespace(engine_reply=SimpleNamespace(reply={"job_id": "chat_" + payload["request_id"].replace("-", ""), "invocation": bound, "deadline_at": "2099-01-01T00:00:00Z"}))
    service = module().GeneralQuestionRoutes(Worker(), SimpleNamespace(enqueue=lambda *a, **k: {"state": "verified"}), policy_digest=EXPECTED["policy_digest"], deployment_digest=EXPECTED["deployment_digest"])
    monkeypatch.setattr(main.app.state, "general_question_routes", service, raising=False)
    client = TestClient(main.app)
    base = {"message": "Follow up", "history": [], "market": "za"}
    assert client.post("/api/chat/send", json=base).status_code == 401
    assert observed == []
    headers = {"X-Passcode": "synthetic-test-passcode"}
    assert client.post("/api/chat/send", json=base, headers=headers).status_code == 202
    assert observed[-1]["contract_version"] == "general_question_admission_v1"
    assert "parent_request_id" not in observed[-1]
    both_null = base | {"parent_request_id": None, "thread_anchor_request_id": None}
    assert client.post("/api/chat/send", json=both_null, headers=headers).status_code == 202
    assert observed[-1]["contract_version"] == "general_question_admission_v1"
    refs = {"parent_request_id": "ef06dcc1-79f5-490d-847f-868866d3ce83", "thread_anchor_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"}
    assert client.post("/api/chat/send", json=base | refs, headers=headers).status_code == 202
    assert observed[-1]["contract_version"] == "general_question_admission_v2"
    assert all(observed[-1][key] == value for key, value in refs.items())
    for extra in ({"parent_request_id": refs["parent_request_id"].upper()}, {"thread_anchor_request_id": refs["thread_anchor_request_id"]}):
        assert client.post("/api/chat/send", json=base | extra, headers=headers).status_code == 400
    for extra in ({"parent_request_id": 1}, {"parent_context_ref": {}}):
        assert client.post("/api/chat/send", json=base | extra, headers=headers).status_code == 422
    assert len(observed) == 3


@pytest.mark.parametrize("parent,anchor,status,detail", [
    ("parent_unavailable", None, 409, "parent_context_unavailable"),
    ("parent_scope_mismatch", None, 409, "parent_context_unavailable"),
    ("parent_source_unavailable", None, 409, "parent_context_unavailable"),
    ("parent_context_conflict", None, 409, "parent_context_conflict"),
    ("parent_read_budget_exhausted", None, 503, "parent_read_budget_exhausted"),
    ("parent_context_deadline", None, 503, "parent_context_deadline"),
])
def test_parent_refusal_surfaces_only_safe_code(monkeypatch, parent, anchor, status, detail):
    from fastapi import HTTPException
    class Worker:
        async def run(self, *args, **kwargs):
            raise WorkerProcessError(parent)
    service = module().GeneralQuestionRoutes(Worker(), object(), policy_digest=EXPECTED["policy_digest"], deployment_digest=EXPECTED["deployment_digest"])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service)))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(module().start_route(request, {"message": "Follow up", "history": [], "market": "za", "parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0"}))
    assert raised.value.status_code == status
    assert raised.value.detail == detail


def test_unconfigured_public_routes_cannot_start_legacy_generation(monkeypatch):
    main = importlib.import_module("src.api.main")
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setattr(main, "_check_rate_limit", lambda request: None)
    monkeypatch.setattr(
        main, "_start_generation_thread", lambda *args: pytest.fail("legacy generation")
    )
    monkeypatch.delattr(main.app.state, "general_question_routes", raising=False)
    with TestClient(main.app) as client:
        assert (
            client.post("/api/chat/send", json={"message": "Question"}).status_code
            == 503
        )
        assert (
            client.get(
                "/api/chat/status", params={"job_id": "chat_" + "0" * 32}
            ).status_code
            == 503
        )


def test_internal_route_has_no_passcode_authentication_substitute(monkeypatch):
    app = FastAPI()
    worker = SimpleNamespace(
        storage_client=None, run=lambda *args: pytest.fail("worker invoked")
    )
    app.state.general_question_routes = module().GeneralQuestionRoutes(
        worker,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    app.add_api_route(
        "/internal/general-question/execute", module().execute_route, methods=["POST"]
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/general-question/execute",
            json=EXPECTED,
            headers={"X-Passcode": "anything"},
        )
    assert response.status_code == 401


def test_authenticated_execute_and_status_use_real_controller_and_verified_storage(
    monkeypatch, tmp_path
):
    worker, store, capacity, _captured = create_worker(tmp_path, operation="execute")
    original_reload, original_download = store.reload, store.download_as_bytes

    def reload_with_worker_budget(**kwargs):
        assert 0 < kwargs["timeout"] <= 240
        return original_reload(**{**kwargs, "timeout": min(10, kwargs["timeout"])})

    def download_with_worker_budget(**kwargs):
        assert 0 < kwargs["timeout"] <= 240
        return original_download(**{**kwargs, "timeout": min(10, kwargs["timeout"])})

    monkeypatch.setattr(store, "reload", reload_with_worker_budget)
    monkeypatch.setattr(store, "download_as_bytes", download_with_worker_budget)
    operations = []

    async def controlled_process(root, interpreter, operation, payload, **kwargs):
        kwargs["verify_runtime"]()
        operations.append((operation, payload, kwargs["deadline"]))
        terminal = receipt()
        await kwargs["verify_result"](
            terminal
            if operation == "execute"
            else EXPECTED
            | {
                "contract_version": "general_question_status_v1",
                "state": "unavailable",
                "deadline_at": "2099-01-01T00:00:00Z",
                "result_digest": HASHES["unavailable"],
                "result_generation": "17",
            }
        )
        return EngineReply(terminal, 0, "0" * 64)

    monkeypatch.setattr(
        "src.api.question_worker_controller.invoke_engine", controlled_process
    )
    monkeypatch.setattr(
        module(),
        "verify_question_worker_authorization",
        lambda headers, **kwargs: {"email": "verified"},
    )
    monkeypatch.setattr(
        module(), "read_request_bytes", lambda *args, **kwargs: raw_request()
    )
    scope = json.loads(raw_request())
    monkeypatch.setattr(
        module(),
        "_current_scope",
        lambda: {
            key: scope[key]
            for key in [
                "client_scope_id",
                "market_scope",
                "brand_config_id",
                "audience_lens_ids",
                "theme_id",
            ]
        },
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat("2026-09-06T20:01:00Z")

    monkeypatch.setattr(module(), "datetime", Clock)
    service = module().GeneralQuestionRoutes(
        worker,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    app = FastAPI()
    app.state.general_question_routes = service
    app.add_api_route(
        "/internal/general-question/execute", module().execute_route, methods=["POST"]
    )
    before = time.monotonic()
    with TestClient(app) as client:
        assert (
            client.post(
                "/internal/general-question/execute",
                json=EXPECTED,
                headers={"Authorization": "synthetic-boundary"},
            ).status_code
            == 204
        )
    assert 179 < operations[0][2] - before < 182
    assert store.reads == ["metadata", "body"]
    response = asyncio.run(
        service.status("chat_" + EXPECTED["request_id"].replace("-", ""))
    )
    assert response["intelligence"]["status"] == "unavailable"
    assert [operation for operation, _, _ in operations] == ["execute", "status"]
    assert not capacity.locked()


@pytest.mark.parametrize(
    "change", [{"extra": "field"}, {"deployment_digest": "f" * 64}]
)
def test_task_body_tampering_refuses_before_request_store(monkeypatch, change):
    monkeypatch.setattr(
        module(), "verify_question_worker_authorization", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(
        module(),
        "read_request_bytes",
        lambda *args, **kwargs: pytest.fail("store read"),
    )
    service = module().GeneralQuestionRoutes(
        SimpleNamespace(storage_client=None),
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    app = FastAPI()
    app.state.general_question_routes = service
    app.add_api_route(
        "/internal/general-question/execute", module().execute_route, methods=["POST"]
    )
    with TestClient(app) as client:
        assert client.post(
            "/internal/general-question/execute", json=EXPECTED | change
        ).status_code in (403, 503)


def test_configured_route_payload_crosses_real_engine_admission_and_status(monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import sys

    configured_engine = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    configured_python = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if bool(configured_engine) != bool(configured_python):
        pytest.fail("Both engine integration test paths must be configured together")
    engine = (
        Path(configured_engine)
        if configured_engine
        else Path(__file__).resolve().parents[3]
        / "trends-engine-source-provenance-v3"
    )
    interpreter = Path(configured_python) if configured_python else Path(sys.executable)
    entrypoint = engine / "scripts/staging/run_general_question_worker.py"
    if configured_engine:
        assert entrypoint.is_file()
        assert interpreter.is_file()
    elif not entrypoint.is_file():
        pytest.skip("Real engine checkout required for cross-boundary regression")
    setup = "from tests.unit.test_general_question_deployment import fixture; store,bucket,invocation,runtime,binding=fixture()"
    identities = subprocess.run(
        [
            interpreter,
            "-c",
            "import json; " + setup + "; print(json.dumps(invocation))",
        ],
        cwd=engine,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    invocation = json.loads(identities.stdout)
    payloads = {}

    class Captured(Exception):
        pass

    class Worker:
        async def run(self, operation, payload, **kwargs):
            from src.api.question_worker_protocol import encode_worker_input

            payloads[operation] = encode_worker_input(operation, payload)
            raise Captured()

    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=invocation["policy_digest"],
        deployment_digest=invocation["deployment_digest"],
    )
    monkeypatch.setattr(module(), "uuid4", lambda: UUID(int=999))
    with pytest.raises(Captured):
        asyncio.run(
            service.start(
                {
                    "message": "What changed in ZA groceries from 1 to 7 September?",
                    "history": [],
                    "market": "za",
                }
            )
        )
    with pytest.raises(Captured):
        asyncio.run(service.status("chat_" + UUID(int=999).hex))
    script = """
import io,json,sys
from scripts.staging.run_general_question_worker import _input
from src.analysis.open_intelligence.general_question_admission import admit_question_transport
from src.analysis.open_intelligence.general_question_request import _scope
from tests.unit.test_general_question_deployment import fixture
from tests.unit.test_general_question_store import NOW
store,bucket,invocation,runtime,binding=fixture()
wire=json.load(sys.stdin)
admission=_input(io.BytesIO(wire['admit'].encode()),'admit')
status=_input(io.BytesIO(wire['status'].encode()),'status')
result=admit_question_transport(admission,store=store,runtime_identity=runtime,now=NOW)
assert _scope(status['scope']) == admission['scope']
context=store.read_request(status['request_id'],scope=status['scope'])
assert context['intake']['selected_market']=='za'
assert context['request']['market_scope']==['ke','ng','za']
assert result['invocation']['request_id']==status['request_id']
print('real_admission_and_status_scope_passed')
"""
    result = subprocess.run(
        [interpreter, "-c", script],
        cwd=engine,
        input=json.dumps({key: value.decode() for key, value in payloads.items()}),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "real_admission_and_status_scope_passed"
class DurableAdmissionWorker:
    """Mirror the engine admission contract: one request identity joins one body."""

    def __init__(self):
        self.durable = {}
        self.calls = []
        self.records = {}

    async def run(self, operation, payload, **kwargs):
        from src.api.question_worker_protocol import encode_worker_input

        encode_worker_input(operation, payload)
        self.calls.append((operation, payload))
        if operation == "admit":
            existing = self.durable.get(payload["request_id"])
            if existing is not None and existing != payload["transport"]:
                raise WorkerProcessError("run_id_conflict")
            self.durable[payload["request_id"]] = payload["transport"]
            bound = {
                key: payload[key]
                for key in ("request_id", "policy_digest", "deployment_digest")
            }
            bound.update(
                contract_version="general_cultural_question_v1",
                request_digest="a" * 64,
                intake_digest="b" * 64,
            )
            reply = {
                "job_id": "chat_" + payload["request_id"].replace("-", ""),
                "invocation": bound,
                "deadline_at": "2099-01-01T00:00:00Z",
            }
            return SimpleNamespace(engine_reply=SimpleNamespace(reply=reply))
        if operation == "status":
            if payload["request_id"] not in self.durable:
                raise WorkerProcessError("request_unknown")
            record = self.records.get(payload["request_id"])
            reply = {"state": "admitted" if record is None else record["state"]}
            reply["deadline_at"] = "2099-01-01T00:00:00Z"
            return SimpleNamespace(
                engine_reply=SimpleNamespace(reply=reply), result_record=record
            )
        raise AssertionError(operation)


def idempotent_service(queue=None):
    worker = DurableAdmissionWorker()
    service = module().GeneralQuestionRoutes(
        worker,
        queue or SimpleNamespace(enqueue=lambda *a, **k: {"state": "verified"}),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    return service, worker


def test_duplicate_submission_under_one_idempotency_key_joins_one_durable_request():
    service, worker = idempotent_service()
    body = {
        "message": "What is changing for Gen Z in South Africa?",
        "history": [],
        "market": "za",
        "idempotency_key": "browser-turn-7f3a9c2e",
    }
    first = asyncio.run(service.start(body))
    second = asyncio.run(service.start(dict(body)))
    assert first == second
    assert len(worker.durable) == 1
    request_id = next(iter(worker.durable))
    assert first["job_id"] == "chat_" + request_id.replace("-", "")
    assert [operation for operation, _ in worker.calls] == ["admit", "admit"]
    assert all(payload["request_id"] == request_id for _, payload in worker.calls)
    assert all("idempotency_key" not in payload for _, payload in worker.calls)
    assert all(
        payload["contract_version"] == "general_question_admission_v1"
        for _, payload in worker.calls
    )


@pytest.mark.parametrize("absent", [{}, {"idempotency_key": None}])
def test_same_text_without_an_idempotency_key_never_joins_by_wording(absent):
    service, worker = idempotent_service()
    body = {"message": "What is changing for Gen Z?", "history": [], "market": "za", **absent}
    first = asyncio.run(service.start(body))
    second = asyncio.run(service.start(dict(body)))
    assert first != second
    assert len(worker.durable) == 2


def test_different_body_under_one_idempotency_key_is_a_conflict():
    from fastapi import HTTPException

    service, worker = idempotent_service()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service))
    )
    body = {
        "message": "First wording",
        "history": [],
        "market": "za",
        "idempotency_key": "turn-00000001",
    }
    asyncio.run(module().start_route(request, body))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(module().start_route(request, body | {"message": "Second wording"}))
    assert raised.value.status_code == 409
    assert raised.value.detail == "request_conflict"
    assert len(worker.durable) == 1


@pytest.mark.parametrize(
    "key",
    [
        "",
        "short",
        "has space here",
        "x" * 129,
        7,
        [],
        "bad/slash/key",
        "é-accented-key",
    ],
)
def test_idempotency_key_grammar_is_strict(key):
    from fastapi import HTTPException

    service, worker = idempotent_service()
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            service.start(
                {
                    "message": "Question",
                    "history": [],
                    "market": "za",
                    "idempotency_key": key,
                }
            )
        )
    assert raised.value.status_code == 400
    assert worker.calls == []


def test_idempotency_identity_is_bound_to_the_resolved_scope(monkeypatch):
    service, worker = idempotent_service()
    body = {
        "message": "Question",
        "history": [],
        "market": "za",
        "idempotency_key": "turn-00000002",
    }
    first = asyncio.run(service.start(body))
    original = module()._current_scope
    monkeypatch.setattr(
        module(),
        "_current_scope",
        lambda: original() | {"client_scope_id": "other_scope"},
    )
    second = asyncio.run(service.start(dict(body)))
    assert first != second
    assert len(worker.durable) == 2


def test_stable_request_id_survives_reload_and_status_surfaces_lifecycle_distinctly():
    service, worker = idempotent_service()
    body = {
        "message": "Question",
        "history": [],
        "market": "za",
        "idempotency_key": "turn-00000003",
    }
    before = asyncio.run(service.start(body))
    request_id = str(UUID(hex=before["job_id"][5:]))
    pending = asyncio.run(service.status(before["job_id"]))
    assert pending["pending"] is True
    assert pending["lifecycle"] == {
        "request_id": request_id,
        "state": "pending",
        "engine_state": "admitted",
        "reason_code": None,
        "deadline_at": "2099-01-01T00:00:00Z",
    }
    after_reload = asyncio.run(service.start(dict(body)))
    assert after_reload == before
    unavailable = {
        "answer": "x",
        "sources": [],
        "error": True,
        "intelligence": {"status": "unavailable"},
    }
    complete = {"answer": "x", "sources": [], "intelligence": {"status": "complete"}}
    for state, response, expected in [
        (
            "held",
            unavailable | {"reason": "request_cancelled"},
            ("held", "request_cancelled"),
        ),
        (
            "unavailable",
            unavailable | {"reason": "evidence_insufficient"},
            ("insufficient_evidence", "evidence_insufficient"),
        ),
        (
            "unavailable",
            unavailable | {"reason": "model_timeout"},
            ("unavailable", "model_timeout"),
        ),
        ("complete", complete, ("complete", None)),
    ]:
        worker.records[request_id] = {"state": state, "response": response}
        surfaced = asyncio.run(service.status(before["job_id"]))
        assert "pending" not in surfaced
        assert surfaced["answer"] == "x"
        assert surfaced["lifecycle"] == {
            "request_id": request_id,
            "state": expected[0],
            "engine_state": state,
            "reason_code": expected[1],
            "deadline_at": "2099-01-01T00:00:00Z",
        }


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("control_conflict", "worker_failed"),
        ("run_id_conflict", "request_conflict"),
        ("request_expired", "request_expired"),
    ],
)
def test_a_failed_admission_records_a_reason_from_the_closed_stage_vocabulary(
    caplog, code, reason
):
    """An admission that fails records a stage reason the engine vocabulary holds.

    A store that runs out of conditional put retries answers control_conflict,
    which is not a stage reason, so the event names worker_failed instead. The
    lifecycle harness mirrors this mapping when it records its own admissions.
    """

    class Refused(Exception):
        def __init__(self, value):
            self.code = value
            super().__init__(value)

    class Worker:
        async def run(self, operation, payload, **kwargs):
            assert operation == "admit"
            raise Refused(code)

    caplog.set_level("INFO", logger="listening_post.question_worker")
    service = module().GeneralQuestionRoutes(
        Worker(),
        SimpleNamespace(enqueue=lambda *a, **k: pytest.fail("dispatched")),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    with pytest.raises(Exception):
        asyncio.run(
            service.start({"message": "A question", "history": [], "market": "za"})
        )
    events = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"deployment_digest":')
    ]
    assert [(e["stage"], e["event"], e["reason_code"]) for e in events] == [
        ("admission", "entered", None),
        ("admission", "failed", reason),
    ]
    assert reason in module()._STAGE_REASON_CODES


def test_admission_and_queue_stage_events_are_logged_without_raw_question(caplog):
    caplog.set_level("INFO", logger="listening_post.question_worker")
    dispatched = []

    def enqueue(invocation, **kwargs):
        dispatched.append(invocation["request_id"])
        return {"state": "verified"}

    service, _worker = idempotent_service(SimpleNamespace(enqueue=enqueue))
    result = asyncio.run(
        service.start(
            {
                "message": "PRIVATE question text",
                "history": [],
                "market": "za",
                "idempotency_key": "turn-00000004",
            }
        )
    )
    events = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"deployment_digest":')
    ]
    assert [(e["stage"], e["event"]) for e in events] == [
        ("admission", "entered"),
        ("admission", "completed"),
        ("queue", "entered"),
        ("queue", "completed"),
    ]
    request_id = str(UUID(hex=result["job_id"][5:]))
    assert all(e["event_version"] == "question_stage_event_v1" for e in events)
    assert all(e["request_id"] == request_id for e in events)
    assert len({e["invocation_id"] for e in events}) == 1
    assert all(e["reason_code"] is None for e in events)
    assert [e["usage_state"] for e in events] == [
        "none",
        "reserved",
        "reserved",
        "reserved",
    ]
    assert all(
        set(e)
        == {
            "event_version",
            "request_id",
            "invocation_id",
            "policy_digest",
            "deployment_digest",
            "stage",
            "event",
            "occurred_at",
            "elapsed_ms",
            "reason_code",
            "usage_state",
        }
        for e in events
    )
    assert "PRIVATE" not in caplog.text
    assert dispatched == [request_id]


def test_stage_reason_codes_match_the_engine_source_of_truth():
    import os
    import subprocess
    from pathlib import Path

    engine = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    interpreter = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if not engine or not interpreter:
        pytest.skip("GENERAL_QUESTION_ENGINE_TEST_ROOT and _PYTHON select the engine checkout")
    assert Path(engine, "src/analysis/open_intelligence/general_question_execution.py").is_file()
    probe = (
        "import json; from src.analysis.open_intelligence import general_question_execution as e; "
        "print(json.dumps({'reasons': sorted(e.STAGE_REASON_CODES), "
        "'fields': sorted(e._STAGE_EVENT_FIELDS), 'version': e.STAGE_EVENT_VERSION, "
        "'usage_states': list(e.USAGE_STATES), 'stages': list(e.STAGES), "
        "'events': list(e.STAGE_EVENTS)}))"
    )
    result = subprocess.run(
        [interpreter, "-c", probe],
        cwd=engine,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    engine_contract = json.loads(result.stdout)
    routes = module()
    assert engine_contract["reasons"] == sorted(routes._STAGE_REASON_CODES)
    assert engine_contract["fields"] == sorted(routes._STAGE_EVENT_FIELDS)
    assert engine_contract["version"] == routes._STAGE_EVENT_VERSION
    assert engine_contract["usage_states"] == list(routes._USAGE_STATES)
    assert set(("admission", "queue")) <= set(engine_contract["stages"])
    assert {"entered", "completed", "failed"} <= set(engine_contract["events"])


def _scope_records(caplog):
    return [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"adapter":')
    ]


@pytest.mark.parametrize("parent", [None, str(UUID(int=41))])
def test_start_records_seven_scope_values_at_the_request_boundary(
    monkeypatch, caplog, parent
):
    from src.api import workspace_scope
    from src.api.dossier_store import canonical_digest

    class Captured(Exception):
        pass

    class Worker:
        async def run(self, operation, payload, **kwargs):
            from src.api.question_worker_protocol import encode_worker_input

            encode_worker_input(operation, payload)
            raise Captured()

    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    monkeypatch.setattr(module(), "uuid4", lambda: UUID(int=7))
    caplog.set_level("INFO", logger="listening_post.question_worker")
    body = {
        "message": "Which commuter routes changed this week?",
        "history": [],
        "market": "za",
    }
    if parent is not None:
        body["parent_request_id"] = parent
    with pytest.raises(Captured):
        asyncio.run(service.start(body))
    scope = {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "question_" + UUID(int=7).hex,
        "contract_version": "general_cultural_question_v1",
    }
    digest = canonical_digest(scope)
    assert _scope_records(caplog) == [
        {
            "record_version": "question_scope_binding_v1",
            "boundary": "request",
            "adapter": "general_cultural_question_v1",
            "request_id": str(UUID(int=7)),
            "scope": scope,
            "scope_digest": digest,
            "reference": None
            if parent is None
            else {"kind": "parent_request", "id": parent},
        }
    ]
    assert set(scope) == set(workspace_scope.SCOPE_FIELDS)


def test_status_records_the_history_boundary_with_the_resolved_run(caplog):
    class Captured(Exception):
        pass

    class Worker:
        async def run(self, operation, payload, **kwargs):
            raise Captured()

    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    caplog.set_level("INFO", logger="listening_post.question_worker")
    with pytest.raises(Captured):
        asyncio.run(service.status("chat_" + UUID(int=9).hex))
    [record] = _scope_records(caplog)
    assert (record["boundary"], record["adapter"]) == (
        "history",
        "general_question_status_v1",
    )
    assert record["scope"]["run_id"] == "question_" + UUID(int=9).hex
    assert record["request_id"] == str(UUID(int=9))


@pytest.mark.parametrize(
    "field, value",
    [
        ("run_id", "question_" + "f" * 32),
        ("contract_version", "general_cultural_question_v0"),
    ],
)
def test_execute_refuses_a_stored_request_whose_run_identity_moved(
    monkeypatch, field, value
):
    from fastapi import HTTPException

    monkeypatch.setattr(
        module(), "verify_question_worker_authorization", lambda *args, **kwargs: {}
    )
    stored = json.loads(raw_request())
    stored[field] = value
    monkeypatch.setattr(
        module(),
        "read_request_bytes",
        lambda *args, **kwargs: json.dumps(stored).encode(),
    )

    class Request:
        headers = SimpleNamespace(getlist=lambda name: ["synthetic"])

        async def stream(self):
            yield json.dumps(EXPECTED).encode()

    service = module().GeneralQuestionRoutes(
        SimpleNamespace(storage_client=None),
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(service.execute(Request()))
    assert raised.value.status_code == 403
    assert raised.value.detail == "Worker request identity is invalid"


def test_chat_send_passes_the_idempotency_key_through_to_admission(monkeypatch):
    from src.api import main

    service, worker = idempotent_service()
    monkeypatch.setattr(main.app.state, "general_question_routes", service, raising=False)
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    browser = TestClient(main.app)
    body = {
        "message": "What is changing for Gen Z in South Africa?",
        "history": [],
        "market": "za",
        "idempotency_key": "browser-turn-7f3a9c2e",
    }
    first = browser.post("/api/chat/send", json=body)
    second = browser.post("/api/chat/send", json=dict(body))
    assert first.status_code == 202, first.text
    assert second.status_code == 202, first.text
    assert first.json() == second.json()
    assert len(worker.durable) == 1
    assert all("idempotency_key" not in payload for _, payload in worker.calls)
    changed = browser.post("/api/chat/send", json=body | {"message": "Second wording"})
    assert (changed.status_code, changed.json()["detail"]) == (409, "request_conflict")
    assert len(worker.durable) == 1
    malformed = browser.post("/api/chat/send", json=body | {"idempotency_key": "short"})
    assert (malformed.status_code, malformed.json()["detail"]) == (400, "Chat idempotency key is invalid")
    unknown = browser.post("/api/chat/send", json=body | {"idempotency": "x"})
    assert unknown.status_code == 422
    without = browser.post("/api/chat/send", json={key: value for key, value in body.items() if key != "idempotency_key"})
    assert without.status_code == 202
    assert len(worker.durable) == 2


def _lens_service(worker):
    return module().GeneralQuestionRoutes(
        worker,
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )


class _CapturedPayload(Exception):
    def __init__(self, payload):
        self.payload = payload
        super().__init__("captured")


class _PayloadWorker:
    async def run(self, operation, payload, **kwargs):
        from src.api.question_worker_protocol import encode_worker_input

        encode_worker_input(operation, payload)
        raise _CapturedPayload(payload)


def _bsa_scope():
    return {
        "client_scope_id": "bsa_pulse",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": "bsa",
        "audience_lens_ids": [],
        "theme_id": None,
    }


def _start_with(monkeypatch, body, *, scope=None):
    service = _lens_service(_PayloadWorker())
    monkeypatch.setattr(module(), "uuid4", lambda: UUID(int=7))
    if scope is not None:
        monkeypatch.setattr(module(), "_current_scope", lambda: dict(scope))
    with pytest.raises(_CapturedPayload) as caught:
        asyncio.run(service.start(dict(body)))
    return caught.value.payload


def test_a_question_without_a_lens_stays_general_42_and_carries_no_lens(monkeypatch, caplog):
    caplog.set_level("INFO", logger="listening_post.question_worker")
    payload = _start_with(
        monkeypatch,
        {"message": "Which commuter routes changed this week?", "history": [], "market": "za"},
    )

    assert "client_lens" not in payload
    assert payload["contract_version"] == "general_question_admission_v1"
    [record] = _scope_records(caplog)
    assert "client_lens" not in record
    assert record["scope"]["client_scope_id"] == "ogilvy_default"


def test_an_authorized_lens_reaches_the_envelope_and_the_request_boundary(
    monkeypatch, caplog
):
    from src.api import client_lenses
    from src.api.dossier_store import canonical_digest

    caplog.set_level("INFO", logger="listening_post.question_worker")
    payload = _start_with(
        monkeypatch,
        {
            "message": "What is being said about Play Your Part?",
            "history": [],
            "market": "za",
            "client_lens_id": "bsa_pulse_lens",
        },
        scope=_bsa_scope(),
    )

    envelope = {
        "lens_binding_version": "client_lens_binding_v1",
        "client_lens_id": "bsa_pulse_lens",
        "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
    }
    assert payload["client_lens"] == envelope
    assert payload["scope"] == _bsa_scope()
    [record] = _scope_records(caplog)
    assert record["client_lens"] == envelope
    assert record["scope_digest"] == canonical_digest(
        {**record["scope"], "client_lens": envelope}
    )
    assert client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse")[0][
        "client_lens_id"
    ] == "bsa_pulse_lens"


@pytest.mark.parametrize(
    "client_lens_id",
    ["bsa", "bsa_pulse", "unregistered_lens", "", "BSA_PULSE_LENS", 7, ["bsa_pulse_lens"]],
)
def test_a_lens_the_scope_does_not_authorize_is_refused_before_admission(
    monkeypatch, client_lens_id
):
    from fastapi import HTTPException

    service = _lens_service(_PayloadWorker())
    monkeypatch.setattr(module(), "_current_scope", lambda: _bsa_scope())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            service.start(
                {
                    "message": "What is being said about Play Your Part?",
                    "history": [],
                    "market": "za",
                    "client_lens_id": client_lens_id,
                }
            )
        )
    assert raised.value.status_code == 400
    assert raised.value.detail == "Chat client lens is invalid"


def test_the_bsa_lens_is_refused_under_the_general_scope(monkeypatch):
    """A lens belongs to one client scope; the default scope cannot name it."""
    from fastapi import HTTPException

    service = _lens_service(_PayloadWorker())
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            service.start(
                {
                    "message": "What is being said about Play Your Part?",
                    "history": [],
                    "market": "za",
                    "client_lens_id": "bsa_pulse_lens",
                }
            )
        )
    assert raised.value.status_code == 400


def test_the_same_question_under_two_lenses_binds_two_different_digests(monkeypatch):
    """One idempotency key under two configurations is two durable requests.

    The previous shape of this test asserted the identity was shared, which is
    the defect: the second caller was handed the first caller's job and therefore
    the other configuration's answer. The lens is part of the durable name now.
    """
    key = "shared-browser-key-0001"
    general = _start_with(
        monkeypatch,
        {
            "message": "What is being said about Play Your Part?",
            "history": [],
            "market": "za",
            "idempotency_key": key,
        },
        scope=_bsa_scope(),
    )
    bound = _start_with(
        monkeypatch,
        {
            "message": "What is being said about Play Your Part?",
            "history": [],
            "market": "za",
            "idempotency_key": key,
            "client_lens_id": "bsa_pulse_lens",
        },
        scope=_bsa_scope(),
    )

    assert general["request_id"] != bound["request_id"]
    assert general["transport"] == bound["transport"]
    assert general["scope"] == bound["scope"]
    assert "client_lens" not in general
    assert bound["client_lens"]["client_lens_id"] == "bsa_pulse_lens"


def test_one_idempotency_key_is_stable_under_one_lens_and_never_crosses_two(monkeypatch):
    key = "shared-browser-key-0002"

    def start(lens_id):
        body = {
            "message": "What is being said about Play Your Part?",
            "history": [],
            "market": "za",
            "idempotency_key": key,
        }
        if lens_id is not None:
            body["client_lens_id"] = lens_id
        return _start_with(monkeypatch, body, scope=_bsa_scope())["request_id"]

    assert start(None) == start(None)
    assert start("bsa_pulse_lens") == start("bsa_pulse_lens")
    assert start(None) != start("bsa_pulse_lens")


def test_a_lensed_durable_name_survives_a_republish_of_the_configuration():
    """Republishing an overlay must not silently re key outstanding requests.

    At any instant the registry maps one lens id to one configuration, so the id
    alone already separates every pair of distinct configurations. Binding the
    configuration digest into the durable name buys no separation and costs a
    permanent instability: every legitimate republish would re key every
    outstanding lensed key into a second durable request, a second job and a
    second cost, and a past request's name would stop being recomputable from
    durable data once the registry moved.
    """
    routes = module()
    key = "shared-browser-key-0003"

    def lens(digest):
        return SimpleNamespace(client_lens_id="bsa_pulse_lens", configuration_digest=digest)

    before = routes._identity_name(_bsa_scope(), key, lens("a" * 64))
    after = routes._identity_name(_bsa_scope(), key, lens("b" * 64))
    assert before == after
    assert "a" * 64 not in before

    # Two lenses are still two names, and the lens is still in the durable name.
    other = SimpleNamespace(
        client_lens_id="bsa_pulse_alt_lens", configuration_digest="a" * 64
    )
    assert routes._identity_name(_bsa_scope(), key, other) != before
    assert routes._identity_name(_bsa_scope(), key, None) != before

    # The shape is declared, so the next change to it is a declared one.
    assert before.split("\n") == [
        "bsa_pulse",
        key,
        routes._LENSED_IDENTITY_VERSION,
        "bsa_pulse_lens",
    ]


def test_a_general_idempotency_key_keeps_the_identity_it_had_before_lenses(monkeypatch):
    """A key that names no lens binds exactly the name it bound before U02."""
    from uuid import uuid5

    routes = module()
    key = "unchanged-browser-key-01"
    payload = _start_with(
        monkeypatch,
        {
            "message": "Which commuter routes changed this week?",
            "history": [],
            "market": "za",
            "idempotency_key": key,
        },
    )

    assert payload["request_id"] == str(
        uuid5(routes._IDEMPOTENCY_NAMESPACE, "ogilvy_default\n" + key)
    )


def test_the_console_roster_is_the_authorized_list_with_general_42_as_default(monkeypatch):
    routes = module()
    monkeypatch.setattr(routes, "_current_scope", lambda: _bsa_scope())
    roster = routes.client_lens_roster()
    assert roster == {
        "contract_version": "client_lens_roster_v1",
        "default_client_lens_id": None,
        "client_scope_id": "bsa_pulse",
        "lenses": [
            {
                "client_lens_id": "bsa_pulse_lens",
                "label": "Brand South Africa Pulse",
                "configuration_digest": (
                    "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"
                ),
            }
        ],
    }
    monkeypatch.undo()
    assert module().client_lens_roster()["lenses"] == []


def test_a_follow_up_under_a_lens_carries_the_same_lens_into_its_envelope(monkeypatch, caplog):
    caplog.set_level("INFO", logger="listening_post.question_worker")
    parent = str(UUID(int=41))
    payload = _start_with(
        monkeypatch,
        {
            "message": "And what changed since then?",
            "history": [],
            "market": "za",
            "parent_request_id": parent,
            "client_lens_id": "bsa_pulse_lens",
        },
        scope=_bsa_scope(),
    )

    assert payload["contract_version"] == "general_question_admission_v2"
    assert payload["parent_request_id"] == parent
    assert payload["client_lens"]["client_lens_id"] == "bsa_pulse_lens"
    [record] = _scope_records(caplog)
    assert record["reference"] == {"kind": "parent_request", "id": parent}
    assert record["client_lens"]["client_lens_id"] == "bsa_pulse_lens"


def test_a_follow_up_that_names_no_lens_is_general_42_rather_than_a_silent_carry_over(
    monkeypatch,
):
    payload = _start_with(
        monkeypatch,
        {
            "message": "And what changed since then?",
            "history": [],
            "market": "za",
            "parent_request_id": str(UUID(int=41)),
        },
        scope=_bsa_scope(),
    )
    assert "client_lens" not in payload


LENS_BINDING = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": (
        "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"
    ),
}


def _lensed_binding_digest(request_id, *, lens):
    from src.api import workspace_scope

    return workspace_scope.ScopeBinding.for_question(
        _bsa_scope(), request_id=request_id, client_lens=lens
    ).scope_digest


class _ReplyWorker:
    def __init__(self, lens):
        self.lens = lens

    async def run(self, operation, payload, **kwargs):
        reply = {"state": "admitted", "deadline_at": None}
        if self.lens is not None:
            reply["client_lens"] = dict(self.lens)
        return SimpleNamespace(engine_reply=SimpleNamespace(reply=reply), result_record=None)


def test_the_status_boundary_records_the_lens_the_engine_says_the_request_ran_under(
    monkeypatch, caplog
):
    """A job id names no lens, so the reply is what the boundary takes it from.

    The boundary is written before the read as well, so a run that never returns
    still leaves a record. That first row states that the lens is not yet known;
    only the row written after the reply says which lens the request ran under.
    """
    request_id = str(UUID(int=9))

    monkeypatch.setattr(module(), "_current_scope", lambda: _bsa_scope())
    caplog.set_level("INFO", logger="listening_post.question_worker")
    for lens in (None, LENS_BINDING):
        caplog.clear()
        asyncio.run(_lens_service(_ReplyWorker(lens)).status("chat_" + UUID(int=9).hex))
        opened, settled = _scope_records(caplog)
        assert opened["boundary"] == settled["boundary"] == "history"
        # The row written before the read claims nothing about the lens.
        assert opened["client_lens"] is None
        assert settled.get("client_lens") == lens
        assert ("client_lens" in settled) is (lens is not None)
        assert settled["scope_digest"] == _lensed_binding_digest(request_id, lens=lens)


def test_a_failed_status_read_never_claims_the_request_ran_as_general_42(
    monkeypatch, caplog
):
    """A read that never returned cannot be recorded as a general 42 read.

    A genuine general 42 read omits the lens key entirely. A failed read must not
    produce that same row, and the original failure must reach the caller rather
    than be masked by the record.
    """
    from src.api import workspace_scope

    class Boom(Exception):
        pass

    class FailingWorker:
        async def run(self, operation, payload, **kwargs):
            raise Boom()

    monkeypatch.setattr(module(), "_current_scope", lambda: _bsa_scope())
    caplog.set_level("INFO", logger="listening_post.question_worker")
    with pytest.raises(Boom):
        asyncio.run(_lens_service(FailingWorker()).status("chat_" + UUID(int=9).hex))
    [record] = _scope_records(caplog)
    assert record["boundary"] == "history"
    # A general 42 record has no lens key at all, so an explicit null is what
    # separates "this ran as general 42" from "the read never said".
    assert "client_lens" in record
    assert record["client_lens"] is None
    general = workspace_scope.ScopeBinding.for_question(
        _bsa_scope(), request_id=str(UUID(int=9)), client_lens=None
    )
    assert record["scope_digest"] == general.scope_digest
    # And the shape a genuine general read writes is still the shape it wrote.
    caplog.clear()
    asyncio.run(_lens_service(_ReplyWorker(None)).status("chat_" + UUID(int=9).hex))
    _opened, settled = _scope_records(caplog)
    assert "client_lens" not in settled


def test_a_malformed_reply_lens_refuses_without_masking_the_recorded_boundary(
    monkeypatch, caplog
):
    """A reply lens the scope binding refuses is a refusal, not a silent record."""
    from fastapi import HTTPException

    class MalformedWorker:
        async def run(self, operation, payload, **kwargs):
            return SimpleNamespace(
                engine_reply=SimpleNamespace(
                    reply={
                        "state": "admitted",
                        "deadline_at": None,
                        "client_lens": {"client_lens_id": "bsa_pulse_lens"},
                    }
                ),
                result_record=None,
            )

    monkeypatch.setattr(module(), "_current_scope", lambda: _bsa_scope())
    caplog.set_level("INFO", logger="listening_post.question_worker")
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_lens_service(MalformedWorker()).status("chat_" + UUID(int=9).hex))
    assert raised.value.status_code == 403
    [record] = _scope_records(caplog)
    assert record["client_lens"] is None


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _signed_request(*, lens=None, as_of=None):
    """A stored request whose recorded digest is the digest of its own bytes.

    The execute boundary may only record what the invocation digest binds, so a
    test that asserts what it records has to feed it bytes that digest verifies
    rather than bytes only the scope fields agree with.
    """
    value = json.loads(raw_request())
    if as_of is not None:
        value["as_of"] = as_of
    if lens is not None:
        value["client_lens"] = dict(lens)
    unsigned = {key: item for key, item in value.items() if key != "request_digest"}
    value["request_digest"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return value, _canonical(value), EXPECTED | {"request_digest": value["request_digest"]}


def _fresh_stamp(seconds_ago=1):
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _execute_service(worker=None):
    return module().GeneralQuestionRoutes(
        SimpleNamespace(storage_client=None) if worker is None else worker,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )


def _bind_execute(monkeypatch, stored, data):
    monkeypatch.setattr(
        module(), "verify_question_worker_authorization", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(module(), "read_request_bytes", lambda *args, **kwargs: data)
    monkeypatch.setattr(
        module(),
        "_current_scope",
        lambda: {
            key: stored[key]
            for key in (
                "client_scope_id",
                "market_scope",
                "brand_config_id",
                "audience_lens_ids",
                "theme_id",
            )
        },
    )


class _ExecuteRequest:
    def __init__(self, invocation):
        self.headers = SimpleNamespace(getlist=lambda name: ["synthetic"])
        self._invocation = invocation

    async def stream(self):
        yield json.dumps(self._invocation).encode()


def test_the_execute_boundary_records_the_lens_the_stored_request_carries(
    monkeypatch, caplog
):
    """The lens comes from request bytes the invocation digest has already bound."""
    from src.api.question_worker_deadline import stored_request_lens

    stored, data, invocation = _signed_request(lens=LENS_BINDING, as_of=_fresh_stamp())
    _bind_execute(monkeypatch, stored, data)

    ran = []

    class Worker:
        storage_client = None

        async def run(self, operation, payload, **kwargs):
            ran.append(operation)
            return SimpleNamespace(engine_reply=None, result_record=None)

    caplog.set_level("INFO", logger="listening_post.question_worker")
    asyncio.run(
        _execute_service(Worker())._execute(
            _ExecuteRequest(invocation), {"request_id": None, "operation": "x"}
        )
    )
    assert ran == ["execute"]
    [record] = _scope_records(caplog)
    assert record["boundary"] == "request"
    assert record["client_lens"] == LENS_BINDING
    assert stored_request_lens(stored) == LENS_BINDING
    without = {key: item for key, item in stored.items() if key != "client_lens"}
    assert stored_request_lens(without) is None


def test_the_execute_boundary_records_nothing_from_bytes_the_digest_refuses(
    monkeypatch, caplog
):
    """A lens injected into the stored request is refused before it is recorded.

    The scope fields, the run id and the contract version still agree, so nothing
    short of the invocation digest catches this. The boundary must therefore be
    written after that digest is checked, not before it.
    """
    from src.api.question_worker_deadline import stored_request_lens
    from src.api.question_worker_protocol import WorkerProtocolError

    stored, _clean, _invocation = _signed_request(as_of=_fresh_stamp())
    tampered = {**stored, "client_lens": dict(LENS_BINDING)}
    data = _canonical(tampered)
    # Only the digest separates these bytes from the ones the invocation names.
    assert stored_request_lens(tampered) == LENS_BINDING
    unsigned = {key: item for key, item in tampered.items() if key != "request_digest"}
    assert hashlib.sha256(_canonical(unsigned)).hexdigest() != tampered["request_digest"]

    _bind_execute(monkeypatch, tampered, data)
    caplog.set_level("INFO", logger="listening_post.question_worker")
    with pytest.raises(WorkerProtocolError):
        asyncio.run(
            _execute_service()._execute(
                _ExecuteRequest(EXPECTED | {"request_digest": tampered["request_digest"]}),
                {"request_id": None, "operation": "x"},
            )
        )
    assert _scope_records(caplog) == []


def test_an_expired_stored_request_still_records_the_boundary_it_carried(
    monkeypatch, caplog
):
    """The deadline branch reaches it only once the digest has already passed."""
    stored, data, invocation = _signed_request(lens=LENS_BINDING)
    _bind_execute(monkeypatch, stored, data)

    class Worker:
        storage_client = None

        async def run(self, operation, payload, **kwargs):
            assert operation == "status"
            return SimpleNamespace(engine_reply=None, result_record={"state": "complete"})

    caplog.set_level("INFO", logger="listening_post.question_worker")
    asyncio.run(
        _execute_service(Worker())._execute(
            _ExecuteRequest(invocation), {"request_id": None, "operation": "x"}
        )
    )
    [record] = _scope_records(caplog)
    assert record["boundary"] == "request"
    assert record["client_lens"] == LENS_BINDING


def test_a_malformed_lens_on_a_stored_request_is_refused_rather_than_recorded():
    from src.api.question_worker_deadline import stored_request_lens
    from src.api.question_worker_protocol import WorkerProtocolError

    for broken in (
        {**LENS_BINDING, "lens_binding_version": "client_lens_binding_v0"},
        {**LENS_BINDING, "configuration_digest": "short"},
        {**LENS_BINDING, "client_lens_id": ""},
        {"client_lens_id": "bsa_pulse_lens"},
        "bsa_pulse_lens",
        None,
    ):
        with pytest.raises(WorkerProtocolError):
            stored_request_lens({"client_lens": broken})


def _observation_service(monkeypatch, stored_lens):
    observed = {
        "coverage": {"state": "complete"},
        "rows": [],
        "observed_state": "complete",
        "reserved_microusd": 0,
        "missing_work": [],
    }

    class Worker:
        storage_client = None

        async def run(self, operation, payload, **kwargs):
            return SimpleNamespace(engine_reply=SimpleNamespace(reply=observed))

    service = _lens_service(Worker())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    request.app.state.general_question_routes = service
    monkeypatch.setattr(module(), "_current_scope", lambda: _bsa_scope())
    monkeypatch.setattr(
        module().question_worker_store,
        "read_observed_question_detail",
        lambda *args, **kwargs: {
            "request_bytes": json.dumps(
                {"question": "q", "history": [], "requested_window": None}
            ),
            "intake_bytes": json.dumps({"selected_market": "za"}),
            "result_record": None,
            "plan_window": None,
            "client_lens": stored_lens,
        },
    )
    return request


def test_the_fieldwork_detail_read_records_the_lens_the_request_was_admitted_under(
    monkeypatch, caplog
):
    request_id = str(UUID(int=9))
    caplog.set_level("INFO", logger="listening_post.question_worker")
    for lens in (None, LENS_BINDING):
        caplog.clear()
        request = _observation_service(monkeypatch, lens)
        asyncio.run(module().read_question_observation(request, request_id))
        [record] = _scope_records(caplog)
        assert record["boundary"] == "history"
        assert record.get("client_lens") == lens
        assert record["scope_digest"] == _lensed_binding_digest(request_id, lens=lens)


def test_the_fieldwork_inventory_read_records_the_scope_whose_questions_it_listed(
    monkeypatch, caplog
):
    """Listing every question in a scope is a boundary, so it stops recording nothing."""
    from src.api import workspace_scope

    caplog.set_level("INFO", logger="listening_post.question_worker")
    request = _observation_service(monkeypatch, None)
    asyncio.run(module().read_question_observation(request, None))
    [record] = _scope_records(caplog)
    assert record["boundary"] == "history"
    assert record["request_id"] is None
    assert record["reference"] == {"kind": "question_inventory", "id": None}
    assert record["scope"]["client_scope_id"] == "bsa_pulse"
    assert record["scope"]["run_id"] == "question_inventory"
    assert "client_lens" not in record
    assert record["scope_digest"] == workspace_scope.ScopeBinding.from_values(
        {
            **_bsa_scope(),
            "run_id": "question_inventory",
            "contract_version": "general_cultural_question_v1",
        }
    ).scope_digest


def test_the_lens_roster_read_records_which_configurations_it_disclosed(
    monkeypatch, caplog
):
    caplog.set_level("INFO", logger="listening_post.question_worker")
    routes = module()
    monkeypatch.setattr(routes, "_current_scope", lambda: _bsa_scope())
    roster = routes.client_lens_roster()
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.getMessage().startswith('{"client_scope_id":')
    ]
    assert records == [
        {
            "client_scope_id": "bsa_pulse",
            "default_client_lens_id": None,
            "offered": [
                {
                    "client_lens_id": "bsa_pulse_lens",
                    "configuration_digest": LENS_BINDING["configuration_digest"],
                }
            ],
            "record_version": "question_lens_roster_read_v1",
        }
    ]
    assert [row["client_lens_id"] for row in roster["lenses"]] == ["bsa_pulse_lens"]


@pytest.mark.parametrize(
    ("reason", "status", "detail"),
    [
        (
            "policy_review_lapsed",
            503,
            {
                "code": "policy_review_lapsed",
                "message": "The question was not admitted. Nothing was written. "
                "The service's pricing review has lapsed and needs renewing "
                "before questions can be answered.",
            },
        ),
        ("worker_exit_failed", 503, "General question admission is unavailable"),
        ("approval_required", 503, "General question admission is unavailable"),
    ],
)
def test_a_lapsed_review_refusal_is_not_reported_as_an_uncertain_admission(
    reason, status, detail
):
    from fastapi import HTTPException

    class Worker:
        async def run(self, *args, **kwargs):
            raise WorkerProcessError(reason)

    service = module().GeneralQuestionRoutes(
        Worker(),
        object(),
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service))
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            module().start_route(
                request, {"message": "Question", "history": [], "market": "za"}
            )
        )
    assert raised.value.status_code == status
    assert raised.value.detail == detail
