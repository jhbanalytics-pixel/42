import copy
import importlib
import io
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from tests.unit import test_general_question_admission as fixture
from tests.unit import test_general_question_store as store_fixture


def cli():
    return importlib.import_module("scripts.staging.run_general_question_worker")


def invoke(raw, *, argv=None, factory=None):
    output, errors = io.BytesIO(), io.BytesIO()
    status = cli().main(
        ["--operation", "admit"] if argv is None else argv,
        stdin=io.BytesIO(raw),
        stdout=output,
        stderr=errors,
        host_factory=factory,
        now=lambda: store_fixture.NOW,
    )
    return status, output.getvalue(), errors.getvalue()


def test_observe_cli_returns_inventory_without_status_or_writes(monkeypatch):
    store, bucket, _, identity, _ = fixture.deployment_fixture.fixture()
    monkeypatch.setattr(
        store_fixture.Blob, "updated", property(lambda self: store_fixture.NOW), raising=False
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Observer used a mutating boundary")

    monkeypatch.setattr(store, "status", forbidden)
    monkeypatch.setattr(bucket, "blob", forbidden)
    value = {
        "contract_version": "general_question_observe_request_v1",
        "scope": store_fixture.scope(),
        "request_id": None,
    }
    status, output, errors = invoke(
        json.dumps(value).encode(),
        argv=["--operation", "observe"],
        factory=lambda **_: {"store": store, "runtime_identity": identity},
    )
    assert status == 0
    assert errors == b""
    result = json.loads(output)
    assert result["mode"] == "inventory"
    assert result["coverage"]["total_count"] == 1
    assert result["rows"][0]["status"] == "unconfirmed"


@pytest.mark.parametrize(
    "change", [{"request_id": "PRIVATE_BAD_ID"}, {"scope": {}}, {"extra": "PRIVATE_DATA"}]
)
def test_observe_bad_input_refuses_before_host_and_never_echoes(change):
    value = {
        "contract_version": "general_question_observe_request_v1",
        "scope": store_fixture.scope(),
        "request_id": None,
        **change,
    }

    def forbidden(**kwargs):
        pytest.fail("Invalid observer input reached host")

    status, output, errors = invoke(
        json.dumps(value).encode(), argv=["--operation", "observe"], factory=forbidden
    )
    assert (status, output) == (1, b"")
    assert json.loads(errors) == {"error": "request_invalid"}


def test_admission_cli_emits_safe_stage_timings_and_counts():
    store, _, invocation, identity, _ = fixture.deployment_fixture.fixture()
    value = fixture.value(
        invocation, request_id=str(UUID(int=91)), transport={"message": "PRIVATE_QUESTION_SENTINEL"}
    )
    value.update(
        contract_version="general_question_admission_v2",
        parent_request_id=str(UUID(int=92)),
        thread_anchor_request_id=None,
    )
    status, _, raw = invoke(
        json.dumps(value).encode(),
        factory=lambda **_: {"store": store, "runtime_identity": identity},
    )
    events = [json.loads(line) for line in raw.splitlines()]
    stages = [
        event
        for event in events
        if event.get("contract_version") == "general_question_admission_stage_v1"
    ]
    assert status == 1
    assert stages
    assert {event["stage"] for event in stages} >= {
        "admission",
        "authority",
        "child_lookup",
        "parent_intake",
    }
    assert stages[-1]["state"] == "failed"
    assert stages[-1]["read_attempts"] > 0
    assert all(
        set(event)
        == {
            "contract_version",
            "request_id",
            "stage",
            "state",
            "error_code",
            "elapsed_ms",
            "read_attempts",
            "metadata_attempts",
            "body_attempts",
        }
        for event in stages
    )
    assert b"PRIVATE_QUESTION_SENTINEL" not in raw
    host = next(event for event in stages if event["stage"] == "host_load")
    assert all(host[key] is None for key in ("read_attempts", "metadata_attempts", "body_attempts"))


def test_invalid_request_id_never_enters_admission_diagnostics():
    store, _, invocation, identity, _ = fixture.deployment_fixture.fixture()
    value = fixture.value(invocation)
    value["request_id"] = "PRIVATE_INVALID_IDENTIFIER"
    status, _, raw = invoke(
        json.dumps(value).encode(),
        factory=lambda **_: {"store": store, "runtime_identity": identity},
    )
    assert status == 1
    assert b"PRIVATE_INVALID_IDENTIFIER" not in raw
    assert json.loads(raw) == {"error": "request_invalid"}


def test_admission_emitter_rejects_unknown_stage_and_redacts_exception():
    from src.analysis.open_intelligence.general_question_parent_context import admission_stage

    events = []
    with admission_stage("PRIVATE_STAGE", request_id=str(UUID(int=91)), diagnostics=events.append):
        pass
    assert events == []
    with (
        pytest.raises(ValueError, match="PRIVATE_EXCEPTION"),
        admission_stage("host_load", request_id=str(UUID(int=91)), diagnostics=events.append),
    ):
        raise ValueError("PRIVATE_EXCEPTION")
    assert events[-1]["error_code"] == "admission_failed"
    assert "PRIVATE" not in json.dumps(events)


@pytest.mark.parametrize(
    "raw",
    [b"{", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"\xff", b"x" * (256 * 1024 + 1)],
    ids=["syntax", "array", "duplicate", "nan", "encoding", "oversized"],
)
def test_invalid_input_refuses_before_host_access_and_never_echoes_payload(raw):
    def forbidden(**_kwargs):
        pytest.fail("host accessed before request validation")

    status, output, errors = invoke(raw, factory=forbidden)
    assert status == 1
    assert output == b""
    assert json.loads(errors) == {"error": "request_invalid"}


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--operation", "unsupported"],
        ["--operation", "admit", "--unsafe"],
    ],
)
def test_unimplemented_or_unknown_operation_never_falls_back_to_admission(argv):
    status, output, errors = invoke(
        b"{}", argv=argv, factory=lambda **_kwargs: pytest.fail("host accessed")
    )
    assert status == 1
    assert output == b""
    assert json.loads(errors) == {"error": "operation_invalid"}


def test_host_failure_has_bounded_secret_free_error():
    def broken(**_kwargs):
        raise RuntimeError("Bearer SECRET synthetic private question")

    payload = {
        "contract_version": "general_question_admission_v1",
        "request_id": "00000000-0000-4000-8000-000000000001",
        "transport": {"message": "Synthetic question"},
        "scope": store_fixture.scope(),
        "selected_market": "za",
        "policy_digest": "a" * 64,
        "deployment_digest": "b" * 64,
    }
    status, output, errors = invoke(json.dumps(payload).encode(), factory=broken)
    assert status == 1
    assert output == b""
    events = [json.loads(line) for line in errors.splitlines()]
    assert events[-1] == {"error": "worker_failed"}
    assert events[-2]["stage"] == "host_load"
    assert events[-2]["error_code"] == "admission_failed"


def test_admission_process_returns_only_durable_metadata_and_reuses_retry():
    store, bucket, invocation, identity, _ = fixture.deployment_fixture.fixture()
    payload = fixture.value(invocation, request_id=str(UUID(int=22)))
    before = copy.deepcopy(payload)

    def factory(**_kwargs):
        return {"store": store, "runtime_identity": identity, "credentials": object()}

    status, output, errors = invoke(json.dumps(payload).encode(), factory=factory)
    assert status == 0
    events = [json.loads(line) for line in errors.splitlines()]
    assert events[-1]["stage"] == "admission"
    assert events[-1]["state"] == "succeeded"
    assert all(event["read_attempts"] is None for event in events)
    result = json.loads(output)
    assert set(result) == {
        "contract_version",
        "job_id",
        "invocation",
        "request_generation",
        "intake_generation",
        "deadline_at",
    }
    assert b"Synthetic" not in output
    assert b"transport" not in output
    assert len(output) <= 65536
    uploads = len(bucket.uploads)
    again = invoke(json.dumps(payload).encode(), factory=factory)
    assert again[:2] == (status, output)
    retry_events = [json.loads(line) for line in again[2].splitlines()]
    assert retry_events[-1]["stage"] == "admission"
    assert retry_events[-1]["state"] == "succeeded"
    assert len(bucket.uploads) == uploads
    assert payload == before


def test_actual_cli_invalid_json_starts_without_cloud_access():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/staging/run_general_question_worker.py"),
            "--operation",
            "admit",
        ],
        input=b"{",
        capture_output=True,
        cwd=root,
        timeout=15,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert json.loads(result.stderr) == {"error": "request_invalid"}


def test_status_process_reads_existing_admission_without_new_work():
    store, bucket, invocation, identity, _ = fixture.deployment_fixture.fixture()
    payload = {
        "contract_version": "general_question_status_request_v1",
        "request_id": invocation["request_id"],
        "scope": store_fixture.scope(),
    }
    uploads = len(bucket.uploads)
    status, output, errors = invoke(
        json.dumps(payload).encode(),
        argv=["--operation", "status"],
        factory=lambda **_: {"store": store, "runtime_identity": identity, "credentials": object()},
    )
    assert status == 0
    assert errors == b""
    result = json.loads(output)
    assert result["contract_version"] == "general_question_status_v1"
    assert result["request_digest"] == invocation["request_digest"]
    assert result["state"] == "admitted"
    assert result["result_digest"] is None
    assert result["result_generation"] is None
    assert len(bucket.uploads) == uploads


def test_status_rejects_extra_fields_before_loading_host():
    payload = {
        "contract_version": "general_question_status_request_v1",
        "request_id": str(UUID(int=1)),
        "scope": store_fixture.scope(),
        "as_of": "forged",
    }
    status, output, errors = invoke(
        json.dumps(payload).encode(),
        argv=["--operation", "status"],
        factory=lambda **_: pytest.fail("host accessed before shape validation"),
    )
    assert status == 1
    assert output == b""
    assert json.loads(errors) == {"error": "request_invalid"}


def test_execute_process_reuses_durable_result_and_emits_only_safe_diagnostics():
    from tests.unit import test_general_question_result as result_fixture

    store, _bucket, invocation, identity, _ = fixture.deployment_fixture.fixture()
    expected = result_fixture.publish(store, invocation["request_id"])
    expected["state"] = "terminal"
    status, output, errors = invoke(
        json.dumps(invocation).encode(),
        argv=["--operation", "execute"],
        factory=lambda **_: {"store": store, "runtime_identity": identity, "credentials": object()},
    )
    assert status == 0
    assert json.loads(output) == expected
    events = [json.loads(line) for line in errors.splitlines()]
    assert [event["state"] for event in events] == ["started", "reused"]
    assert all(event["request_id"] == invocation["request_id"] for event in events)
    assert all(
        set(event) == {"contract_version", "request_id", "phase", "state", "code", "elapsed_ms"}
        for event in events
    )


@pytest.mark.parametrize("injected", [False, True])
def test_execute_clock_is_sampled_after_import_and_event_loop_start(monkeypatch, injected):
    import asyncio
    import builtins
    from datetime import timedelta

    subject = cli()
    execution = importlib.import_module("src.analysis.open_intelligence.general_question_execution")
    store, _, invocation, identity, _ = fixture.deployment_fixture.fixture()
    clock = [store_fixture.NOW]
    original_import = builtins.__import__

    def delayed_import(name, *args, **kwargs):
        result = original_import(name, *args, **kwargs)
        if name == "src.analysis.open_intelligence.general_question_execution":
            clock[0] += timedelta(seconds=4)
        return result

    original_run = asyncio.run

    def delayed_loop(coro):
        clock[0] += timedelta(seconds=2)
        return original_run(coro)

    class Clock:
        @staticmethod
        def now(zone):
            return clock[0]

    observed = []

    async def execute(value, **kwargs):
        from src.analysis.open_intelligence.general_question_context_admission import (
            _ISOLATED_READER_EXECUTION,
        )

        assert _ISOLATED_READER_EXECUTION.get() is True
        assert await asyncio.to_thread(_ISOLATED_READER_EXECUTION.get) is True
        observed.append(kwargs["now"])
        assert kwargs["now"] == clock[0]
        context = store.read_request(value["request_id"], scope=store_fixture.scope())
        assert context["admission"]["deadline_at"] == "2026-09-06T20:04:00.000000Z"
        assert (store_fixture.NOW + timedelta(seconds=180) - kwargs["now"]).total_seconds() == 174
        return {"state": "unavailable"}

    monkeypatch.setattr(builtins, "__import__", delayed_import)
    monkeypatch.setattr(asyncio, "run", delayed_loop)
    monkeypatch.setattr(subject, "datetime", Clock)
    monkeypatch.setattr(execution, "execute_general_question", execute)
    output, errors = io.BytesIO(), io.BytesIO()
    result = subject.main(
        ["--operation", "execute"],
        stdin=io.BytesIO(json.dumps(invocation).encode()),
        stdout=output,
        stderr=errors,
        host_factory=lambda **kwargs: {
            "store": store,
            "runtime_identity": identity,
            "credentials": object(),
        },
        now=(lambda: clock[0]) if injected else None,
    )
    assert result == 0, errors.getvalue()
    assert observed == [store_fixture.NOW + timedelta(seconds=6)]
    from src.analysis.open_intelligence.general_question_context_admission import (
        _ISOLATED_READER_EXECUTION,
    )

    assert _ISOLATED_READER_EXECUTION.get() is False


@pytest.mark.parametrize(
    "code", ["snapshot_cap_version_unknown", "protected_context_registry_invalid"]
)
def test_snapshot_cap_refusal_survives_cli_and_admission_diagnostic(code):
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    def broken(**kwargs):
        raise QuestionStoreError(code)

    _, _, invocation, _, _ = fixture.deployment_fixture.fixture()
    status, output, errors = invoke(json.dumps(fixture.value(invocation)).encode(), factory=broken)
    events = [json.loads(line) for line in errors.splitlines()]
    assert (status, output) == (1, b"")
    assert events[-1] == {"error": code}
    assert events[-2]["error_code"] == code
