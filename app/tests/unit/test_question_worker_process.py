import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.question_worker_process import (
    _WorkerDiagnosticDrain,
    WorkerProcessError,
    invoke_engine,
)
from tests.unit.test_question_worker_protocol import (
    execution_reply,
    invocation,
    status_reply,
    status_request,
)

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="Cloud Run process groups are tested on Linux"
)


def test_admission_stage_diagnostic_survives_drain_and_rejects_unknown(caplog):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = {
        "contract_version": "general_question_admission_stage_v1",
        "request_id": invocation()["request_id"],
        "stage": "context_prefetch",
        "state": "succeeded",
        "error_code": None,
        "elapsed_ms": 812,
        "read_attempts": 9,
        "metadata_attempts": 9,
        "body_attempts": 8,
    }
    drain = _WorkerDiagnosticDrain(event["request_id"], "admit")
    invalid = [
        dict(event, stage="PRIVATE_TEXT"),
        dict(event, question="PRIVATE_TEXT"),
        dict(event, elapsed_ms=True),
        dict(event, read_attempts=41),
        dict(event, error_code="PRIVATE_TEXT"),
        dict(event, request_id="wrong"),
        dict(event, body_attempts=-1),
        dict(event, read_attempts=None),
        dict(event, body_attempts=10),
        dict(event, metadata_attempts=8),
        dict(event, state="failed"),
    ]
    for value in [*invalid, event]:
        drain.feed(json.dumps(value).encode() + b"\n")
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ]
    assert records == [event]
    assert "PRIVATE_TEXT" not in caplog.text


@pytest.mark.parametrize(
    ("stage", "state", "error_code"),
    [
        ("policy_freshness", "failed", "approval_required"),
        ("intake_context", "succeeded", None),
    ],
)
def test_admission_stages_before_the_reservation_reach_the_log(
    caplog, stage, state, error_code
):
    """A lapsed pricing review and the intake build are logged, not an unexplained gap."""
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = {
        "contract_version": "general_question_admission_stage_v1",
        "request_id": invocation()["request_id"],
        "stage": stage,
        "state": state,
        "error_code": error_code,
        "elapsed_ms": 6533,
        "read_attempts": None,
        "metadata_attempts": None,
        "body_attempts": None,
    }
    _WorkerDiagnosticDrain(event["request_id"], "admit").feed(
        json.dumps(event).encode() + b"\n"
    )
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ]
    assert records == [event]


def test_host_stage_preserves_unknown_counts_and_uses_console_sink(capsys, monkeypatch):
    from src.api import question_worker_process as worker

    monkeypatch.setattr(worker._DIAGNOSTIC_LOGGER, "handlers", [])
    worker.configure_worker_diagnostic_logging()
    event = {
        "contract_version": "general_question_admission_stage_v1",
        "request_id": invocation()["request_id"],
        "stage": "host_load",
        "state": "succeeded",
        "error_code": None,
        "elapsed_ms": 10,
        "read_attempts": None,
        "metadata_attempts": None,
        "body_attempts": None,
    }
    _WorkerDiagnosticDrain(event["request_id"], "admit").feed(
        json.dumps(event).encode() + b"\n"
    )
    assert json.loads(capsys.readouterr().err) == event


def bundle(tmp_path, source):
    entry = tmp_path / "scripts" / "staging" / "run_general_question_worker.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(source, encoding="utf-8")
    return tmp_path


def diagnostic_record(**changes):
    return {
        "contract_version": "general_question_diagnostic_v1",
        "request_id": invocation()["request_id"],
        "phase": "planning",
        "state": "started",
        "code": None,
        "elapsed_ms": 7,
        **changes,
    }


def model_failure_record(**changes):
    return {
        "contract_version": "general_question_model_failure_v1",
        "request_id": invocation()["request_id"],
        "stage": "planning",
        "exception_class": "QuestionStoreError",
        "error_code": "model_timeout",
        "provider_status": None,
        "provider_status_label": None,
        "provider_message": None,
        **changes,
    }


def retrieval_cause_record(**changes):
    return {
        "contract_version": "general_question_retrieval_cause_v1",
        "request_id": invocation()["request_id"],
        "phase": "retrieval",
        "public_code": "retrieval_incomplete",
        "exception_type": "QuestionStoreError",
        "exception_code": "plan_invalid",
        "builder_status": "refused",
        "builder_reason": "plan_invalid",
        "builder_missing_work": ["source_coverage", "claim_support"],
        "builder_missing_work_omitted": 3,
        "elapsed_ms": 4120,
        **changes,
    }


def test_parent_exception_inspection_never_renders_provider_objects():
    from src.api.question_worker_process import parent_failure_code

    class ErrorWithBrokenCode(Exception):
        @property
        def code(self):
            raise RuntimeError("private-provider-secret")

    assert parent_failure_code(ErrorWithBrokenCode()) == "worker_failed"


def test_fresh_uvicorn_startup_emits_safe_diagnostic_json_once():
    record = diagnostic_record()
    source = """
import asyncio,json,logging
import uvicorn
uvicorn.Config('src.api.main:app')
assert logging.getLogger().handlers == []
from src.api import main
from src.api.question_worker_process import _WorkerDiagnosticDrain
main.general_question_startup.configure_general_question_startup=lambda *args,**kwargs:None
asyncio.run(main._configure_general_question_worker())
asyncio.run(main._configure_general_question_worker())
record=RECORD
_WorkerDiagnosticDrain(record['request_id']).feed(b'private-secret-sentinel\\n'+json.dumps(record).encode()+b'\\n')
logging.getLogger('listening_post.question_worker').warning('question_enqueue_unresolved request_id=%s',record['request_id'])
""".replace("RECORD", repr(record))
    env = {**os.environ, "DEPLOYMENT_PROFILE": "", "GENERAL_QUESTION_ENABLED": "false"}
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    lines = result.stderr.decode("utf-8").splitlines()
    diagnostics = [json.loads(line) for line in lines if line.startswith('{"code":')]
    assert diagnostics == [record]
    assert sum("question_enqueue_unresolved" in line for line in lines) == 1
    assert "secret" not in result.stderr.decode("utf-8")
    assert result.stdout == b""


def test_console_sink_failure_is_nonfatal_and_preserves_capture(caplog):
    from src.api import question_worker_process as worker

    caplog.set_level(logging.INFO, logger="listening_post.question_worker")

    class BrokenStream:
        def write(self, message):
            raise OSError("private sink exception")

        def flush(self):
            pass

    handler = worker._WorkerDiagnosticStreamHandler(BrokenStream())
    handler.setFormatter(logging.Formatter("%(message)s"))
    worker._DIAGNOSTIC_LOGGER.addHandler(handler)
    try:
        worker._WorkerDiagnosticDrain(invocation()["request_id"]).feed(
            json.dumps(diagnostic_record()).encode() + b"\n"
        )
    finally:
        worker._DIAGNOSTIC_LOGGER.removeHandler(handler)
    assert [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == [diagnostic_record()]
    assert "private sink" not in caplog.text


def stream_diagnostics(chunks):
    from src.api.question_worker_process import _WorkerDiagnosticDrain

    async def run():
        source = "import sys,time\n" + "\n".join(
            f"sys.stderr.buffer.write({chunk!r});sys.stderr.buffer.flush();time.sleep(.01)"
            for chunk in chunks
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        diagnostics = _WorkerDiagnosticDrain(invocation()["request_id"])
        observed = bytearray()
        while chunk := await process.stderr.read(37):
            observed.extend(chunk)
            diagnostics.feed(chunk)
        await process.wait()
        assert process.returncode == 0
        assert await process.stdout.read() == b""
        return bytes(observed)

    return asyncio.run(run())


def test_real_subprocess_split_diagnostic_is_reserialized_without_raw_stderr(caplog):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    record = diagnostic_record()
    encoded = json.dumps(record, indent=None).encode() + b"\n"
    chunks = [
        b"private-secret-sentinel\n",
        encoded[:30],
        encoded[30:80],
        encoded[80:],
        b"traceback raw-secret\n",
    ]
    observed = stream_diagnostics(chunks)
    records = [
        item.getMessage()
        for item in caplog.records
        if item.name == "listening_post.question_worker"
    ]
    assert [json.loads(item) for item in records] == [record]
    assert records[0] == json.dumps(record, sort_keys=True, separators=(",", ":"))
    assert "secret" not in caplog.text
    assert observed == b"".join(chunks)
    assert (
        hashlib.sha256(observed).digest() == hashlib.sha256(b"".join(chunks)).digest()
    )


def test_actual_engine_model_failure_line_reaches_worker_logging_sink(caplog):
    caplog.set_level(logging.WARNING, logger="listening_post.question_worker")
    configured_engine = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_ROOT")
    configured_python = os.environ.get("GENERAL_QUESTION_ENGINE_TEST_PYTHON")
    if bool(configured_engine) != bool(configured_python):
        pytest.fail("Both engine integration test paths must be configured together")
    engine = (
        Path(configured_engine)
        if configured_engine
        else Path(__file__).resolve().parents[3] / "trends-engine-source-provenance-v3"
    )
    interpreter = Path(configured_python) if configured_python else Path(sys.executable)
    if configured_engine:
        assert (
            engine / "src/analysis/open_intelligence/general_question_runtime.py"
        ).is_file()
        assert interpreter.is_file()
    elif not (
        engine / "src/analysis/open_intelligence/general_question_runtime.py"
    ).is_file():
        pytest.skip("Real engine checkout required for model failure bridge regression")
    source = """
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_runtime import _log_model_failure
_log_model_failure(QuestionStoreError('model_timeout'), request_id=REQUEST_ID, stage='planning')
""".replace("REQUEST_ID", repr(invocation()["request_id"]))
    result = subprocess.run(
        [interpreter, "-c", source],
        cwd=engine,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout == b""
    emitted = json.loads(result.stderr)
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(result.stderr)
    forwarded = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ]
    assert forwarded == [emitted] == [model_failure_record()]


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "00000000-0000-0000-0000-000000000002"},
        {"extra": "private-secret-sentinel"},
        {"stage": ["planning"]},
        {"exception_class": False},
        {"exception_class": "x" * 101},
        {"error_code": "private_error"},
        {"provider_status": True},
        {"provider_status": 600},
        {"provider_status_label": "mixedCase"},
        {"provider_status_label": "X" * 65},
        {"provider_message": False},
        {"provider_message": "é" * 501},
    ],
)
def test_model_failure_diagnostic_rejects_foreign_oversize_extra_and_untyped_fields(
    caplog, changes
):
    caplog.set_level(logging.WARNING, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(model_failure_record(**changes)).encode("utf-8") + b"\n"
    )
    assert [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == []


@pytest.mark.parametrize(
    "line",
    [
        b'{"contract_version":"general_question_model_failure_v1"}\n',
        b'{"contract_version":"general_question_model_failure_v1"\n',
        b"x" * 2049 + b"\n",
    ],
)
def test_model_failure_diagnostic_rejects_missing_malformed_and_overlong_lines(
    caplog, line
):
    caplog.set_level(logging.WARNING, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(line)
    assert [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == []


def test_retrieval_cause_diagnostic_is_forwarded_at_warning_with_all_fields(caplog):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record()
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    forwarded = [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ]
    assert [record.levelno for record in forwarded] == [logging.WARNING]
    assert [json.loads(record.message) for record in forwarded] == [event]
    assert set(json.loads(forwarded[0].message)) == {
        "contract_version",
        "request_id",
        "phase",
        "public_code",
        "exception_type",
        "exception_code",
        "builder_status",
        "builder_reason",
        "builder_missing_work",
        "builder_missing_work_omitted",
        "elapsed_ms",
    }
    assert forwarded[0].message == json.dumps(
        event, sort_keys=True, separators=(",", ":")
    )


def test_retrieval_cause_diagnostic_accepts_the_bare_cause_shape(caplog):
    caplog.set_level(logging.WARNING, logger="listening_post.question_worker")
    event = retrieval_cause_record(
        exception_code=None,
        builder_status=None,
        builder_reason=None,
        builder_missing_work=None,
        builder_missing_work_omitted=0,
        elapsed_ms=0,
    )
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == [event]


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "private-secret-sentinel"},
        {"exception_message": "private provider text"},
        {"contract_version": "general_question_retrieval_cause_v2"},
        {"contract_version": "general_question_diagnostic_v1"},
        {"request_id": "00000000-0000-0000-0000-000000000002"},
        {"phase": "planning_private"},
        {"phase": ["retrieval"]},
        {"public_code": "private_public_code"},
        {"public_code": None},
        {"exception_type": ""},
        {"exception_type": "X" * 101},
        {"exception_type": None},
        {"exception_code": "Private Text"},
        {"exception_code": "9starts_with_digit"},
        {"exception_code": "x" * 65},
        {"exception_code": 7},
        {"builder_status": "REFUSED"},
        {"builder_reason": "plan invalid"},
        {"builder_reason": "plan_invalid\n"},
        {"builder_missing_work": ["a"] * 9},
        {"builder_missing_work": ["source_coverage", "Private Text"]},
        {"builder_missing_work": ["source_coverage", None]},
        {"builder_missing_work": "source_coverage"},
        {"builder_missing_work_omitted": -1},
        {"builder_missing_work_omitted": 1001},
        {"builder_missing_work_omitted": True},
        {"builder_missing_work_omitted": None},
        {"elapsed_ms": -1},
        {"elapsed_ms": 210001},
        {"elapsed_ms": 12.0},
    ],
)
def test_retrieval_cause_diagnostic_rejects_foreign_extra_and_untyped_fields(
    caplog, changes
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(retrieval_cause_record(**changes)).encode("utf-8") + b"\n"
    )
    assert [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == []
    assert "private" not in caplog.text.lower()


def test_retrieval_cause_diagnostic_rejects_a_missing_field(caplog):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record()
    del event["builder_missing_work_omitted"]
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == []


def test_retrieval_cause_diagnostic_needs_a_uuid_request_id_even_when_it_matches(
    caplog,
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record(request_id="not-a-uuid")
    _WorkerDiagnosticDrain("not-a-uuid").feed(json.dumps(event).encode("utf-8") + b"\n")
    assert [
        record
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ] == []


# The engine's general_question_retrieval_cause_v1 line for a refusal whose
# missing_work held nothing code shaped, captured byte for byte from engine ab802ee.
ENGINE_EMPTY_MISSING_WORK_CAUSE_LINE = (
    b'{"builder_missing_work":[],"builder_missing_work_omitted":0,'
    b'"builder_reason":"coverage_incomplete","builder_status":"coverage_gap",'
    b'"contract_version":"general_question_retrieval_cause_v1","elapsed_ms":355,'
    b'"exception_code":"retrieval_incomplete","exception_type":"_RetrievalIncomplete",'
    b'"phase":"retrieval","public_code":"retrieval_incomplete",'
    b'"request_id":"00000000-0000-0000-0000-000000000001"}'
)


def forwarded_retrieval_causes(caplog):
    return [
        (record.levelno, record.message)
        for record in caplog.records
        if record.name == "listening_post.question_worker"
    ]


def test_retrieval_cause_forwards_the_engine_empty_missing_work_line_byte_for_byte(
    caplog,
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    engine_line = ENGINE_EMPTY_MISSING_WORK_CAUSE_LINE
    request_id = json.loads(engine_line)["request_id"]
    drain = _WorkerDiagnosticDrain(request_id, "execute")
    for offset in range(0, len(engine_line) + 1, 37):
        drain.feed((engine_line + b"\n")[offset : offset + 37])
    forwarded = forwarded_retrieval_causes(caplog)
    assert [level for level, _ in forwarded] == [logging.WARNING]
    assert forwarded[0][1].encode("utf-8") == engine_line
    assert json.loads(forwarded[0][1])["builder_missing_work"] == []


@pytest.mark.parametrize("omitted", [0, 2, 1000])
def test_retrieval_cause_keeps_an_empty_missing_work_list_intact(caplog, omitted):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record(
        builder_missing_work=[], builder_missing_work_omitted=omitted
    )
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    forwarded = forwarded_retrieval_causes(caplog)
    assert [level for level, _ in forwarded] == [logging.WARNING]
    logged = json.loads(forwarded[0][1])
    assert logged == event
    assert logged["builder_missing_work"] == []
    assert logged["builder_missing_work_omitted"] == omitted


@pytest.mark.parametrize(
    "exception_type",
    [
        "QuestionStoreError",
        "_RetrievalIncomplete",
        "RuntimeError",
        "Error2",
        "X" * 100,
    ],
)
def test_retrieval_cause_accepts_an_identifier_exception_type(caplog, exception_type):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record(exception_type=exception_type)
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert [
        json.loads(message) for _, message in forwarded_retrieval_causes(caplog)
    ] == [event]


@pytest.mark.parametrize(
    "exception_type",
    [
        "SELECT * FROM users",
        "private-passcode-0000",
        "private passcode",
        "Question Store Error",
        "QuestionStoreError\n",
        "QuestionStoreError.",
        "QuestionStoreError-1",
        "1Error",
        "Erroré",
        "é" * 100,
        " ",
        "x" * 99 + "é",
    ],
)
def test_retrieval_cause_drops_a_non_identifier_exception_type(caplog, exception_type):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(retrieval_cause_record(exception_type=exception_type)).encode(
            "utf-8"
        )
        + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []
    assert "private" not in caplog.text.lower()
    assert "select" not in caplog.text.lower()


@pytest.mark.parametrize(
    "exception_type",
    ["QuestionStoreError", "ValueError", "_PrivateError", "TimeoutError"],
)
def test_retrieval_cause_accepts_an_exception_class_name(caplog, exception_type):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record(exception_type=exception_type)
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert [
        json.loads(message) for _, message in forwarded_retrieval_causes(caplog)
    ] == [event]


# type(error).__name__ is the only writer of exception_type, so an identifier that is
# not shaped like a Python exception class name (a lower case word, a keyword, a bare
# underscore run, a non-ASCII upper case lead) is free text and never logged.
@pytest.mark.parametrize(
    "exception_type",
    [
        "hunter2",
        "class",
        "None",
        "import",
        "match",
        "_",
        "____",
        "_1Error",
        "x" * 100,
        "\u00c9clair",
    ],
)
def test_retrieval_cause_drops_an_identifier_that_is_not_a_class_name(
    caplog, exception_type
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(retrieval_cause_record(exception_type=exception_type)).encode(
            "utf-8"
        )
        + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []
    assert "hunter" not in caplog.text.lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"public_code": ["retrieval_incomplete"]},
        {"public_code": {"retrieval_incomplete": 1}},
        {"phase": ["retrieval"]},
        {"phase": {"retrieval": 1}},
    ],
)
def test_retrieval_cause_drops_unhashable_public_code_and_phase_without_raising(
    caplog, changes
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(retrieval_cause_record(**changes)).encode("utf-8") + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []


@pytest.mark.parametrize("phase", ["execution", "planning", "answering", "result"])
def test_retrieval_cause_is_only_accepted_from_the_retrieval_phase(caplog, phase):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record(phase=phase)
    _WorkerDiagnosticDrain(event["request_id"]).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []


@pytest.mark.parametrize("operation", ["admit", "status", "observe"])
def test_retrieval_cause_is_only_accepted_by_the_execute_drain(caplog, operation):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    event = retrieval_cause_record()
    _WorkerDiagnosticDrain(event["request_id"], operation).feed(
        json.dumps(event).encode("utf-8") + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"builder_missing_work": "abcdefgh"},
        {"builder_missing_work": {"a": 1}},
        {"builder_status": "Refused"},
        {"builder_reason": "planInvalid"},
        {"exception_code": "plan_Invalid"},
    ],
)
def test_retrieval_cause_rejects_list_shaped_strings_and_upper_case_codes(
    caplog, changes
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    _WorkerDiagnosticDrain(invocation()["request_id"]).feed(
        json.dumps(retrieval_cause_record(**changes)).encode("utf-8") + b"\n"
    )
    assert forwarded_retrieval_causes(caplog) == []


def test_fixed_early_child_error_is_correlated_but_arbitrary_error_text_is_discarded(
    caplog,
):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    stream_diagnostics(
        [
            b'{"error":"storage_unavailable"}\n',
            b'{"error":"private-provider-secret"}\n',
            b'{"error":"storage_unavailable","request_id":"forged"}\n',
        ]
    )
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.name == "listening_post.question_worker"
    ]
    assert len(records) == 1
    assert records[0]["request_id"] == invocation()["request_id"]
    assert records[0]["operation"] == "execution"
    assert records[0]["code"] == "storage_unavailable"
    assert "secret" not in caplog.text


def test_admission_parent_error_is_bounded_and_conflicting_errors_stay_generic():
    drain = _WorkerDiagnosticDrain(invocation()["request_id"], "admit")
    drain.feed(b'{"error":"parent_unavailable"}\n')
    drain.feed(b'{"error":"private-provider-secret"}\n')
    assert drain.parent_error == "parent_unavailable"
    drain.feed(b'{"error":"parent_context_conflict"}\n')
    assert drain.parent_error is None
    other = _WorkerDiagnosticDrain(invocation()["request_id"], "status")
    other.feed(b'{"error":"parent_unavailable"}\n')
    assert other.parent_error is None


@pytest.mark.parametrize(
    "version,expected",
    [
        ("v2", "parent_unavailable"),
        ("v1", "worker_exit_failed"),
        ("v2_conflict", "worker_exit_failed"),
    ],
)
def test_actual_child_parent_error_propagates_only_for_v2_admission(
    tmp_path, monkeypatch, version, expected
):
    from src.api import question_worker_process as worker
    from tests.unit.test_question_worker_protocol import admission

    payload = admission()
    if version.startswith("v2"):
        payload.update(
            contract_version="general_question_admission_v2",
            parent_request_id="ad30efed-dd99-49da-8113-3943e289fba0",
            thread_anchor_request_id=None,
        )
    errors = '{"error":"parent_unavailable"}\n'
    if version == "v2_conflict":
        errors += '{"error":"parent_context_conflict"}\n'
    root = bundle(
        tmp_path,
        "import sys,json\nvalue=json.load(sys.stdin)\nassert value == "
        + repr(payload)
        + "\nsys.stderr.write("
        + repr(errors)
        + ")\nsys.exit(1)\n",
    )
    native = asyncio.create_subprocess_exec

    async def portable_launch(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return await native(*args, **kwargs)

    async def portable_stop(process):
        if process.returncode is None:
            process.kill()
        await process.wait()

    monkeypatch.setattr(worker, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(worker, "_terminate_group", portable_stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", portable_launch)

    async def run():
        return await invoke_engine(
            root,
            Path(sys.executable),
            "admit",
            payload,
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
        )

    with pytest.raises(WorkerProcessError, match=expected):
        asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "forged_id",
        "extra",
        "phase",
        "state",
        "code",
        "bool_elapsed",
        "negative_elapsed",
        "large_elapsed",
        "duplicate",
        "malformed",
        "overlong",
        "truncated",
    ],
)
def test_real_subprocess_invalid_lines_do_not_leak_or_salvage_suffix(caplog, case):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    changes = {
        "forged_id": {"request_id": "00000000-0000-0000-0000-000000000002"},
        "extra": {"secret": "private-secret-sentinel"},
        "phase": {"phase": "secret-phase"},
        "state": {"state": "secret-state"},
        "code": {"code": "raw-secret-exception"},
        "bool_elapsed": {"elapsed_ms": True},
        "negative_elapsed": {"elapsed_ms": -1},
        "large_elapsed": {"elapsed_ms": 210001},
    }
    invalid = json.dumps(diagnostic_record(**changes.get(case, {}))).encode()
    if case == "duplicate":
        invalid = invalid[:-1] + b',"code":"secret"}'
    elif case == "malformed":
        invalid = b"private-secret-sentinel {bad json}"
    elif case == "overlong":
        invalid = b" " * 2049 + invalid
    elif case == "truncated":
        stream_diagnostics([invalid])
        assert caplog.records == []
        return
    valid = json.dumps(
        diagnostic_record(phase="result", state="held", code="storage_unavailable")
    ).encode()
    stream_diagnostics([invalid[:100], invalid[100:] + b"\n", valid + b"\n"])
    messages = [
        item.getMessage()
        for item in caplog.records
        if item.name == "listening_post.question_worker"
    ]
    assert [json.loads(item) for item in messages] == [json.loads(valid)]
    assert "secret" not in caplog.text


@pytest.mark.parametrize("mode", ["success", "overflow", "sink_failure"])
def test_real_worker_drain_preserves_byte_hash_and_limit_with_platform_adapter(
    tmp_path, monkeypatch, caplog, mode
):
    from src.api import question_worker_process as worker

    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    record = diagnostic_record()
    stderr = b"raw-secret-sentinel\n" + json.dumps(record).encode() + b"\n"
    if mode == "overflow":
        stderr += b"private" * 50000
    root = bundle(
        tmp_path,
        "import sys,json\njson.load(sys.stdin)\nsys.stderr.buffer.write("
        + repr(stderr)
        + ");sys.stderr.buffer.flush()\nprint(json.dumps("
        + repr(execution_reply())
        + "))\n",
    )
    native = asyncio.create_subprocess_exec

    processes = []

    async def portable_launch(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        process = await native(*args, **kwargs)
        processes.append(process)
        return process

    async def portable_stop(process):
        if process.returncode is None:
            process.kill()
        await process.wait()

    monkeypatch.setattr(worker, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(worker, "_terminate_group", portable_stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", portable_launch)
    if mode == "sink_failure":
        monkeypatch.setattr(
            worker._DIAGNOSTIC_LOGGER,
            "info",
            lambda *args: (_ for _ in ()).throw(RuntimeError("sink unavailable")),
        )

    async def accepted(_reply):
        return True

    async def run():
        return await invoke_engine(
            root,
            Path(sys.executable),
            "execute",
            invocation(),
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
            verify_result=accepted,
        )

    if mode == "overflow":
        with pytest.raises(WorkerProcessError, match="worker_stderr_limit"):
            asyncio.run(run())
    else:
        result = asyncio.run(run())
        assert result.stderr_observed_bytes == len(stderr)
        assert result.stderr_observed_sha256 == hashlib.sha256(stderr).hexdigest()
        assert result.reply == execution_reply()
    assert "secret" not in caplog.text
    assert len(processes) == 1
    transport = processes[0]._transport
    assert processes[0].returncode is not None
    assert transport.is_closing()
    assert all(transport.get_pipe_transport(fd).is_closing() for fd in (0, 1, 2))


def test_diagnostic_newline_byte_counts_toward_exact_line_limit(caplog):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    line = json.dumps(diagnostic_record()).encode()
    accepted = b" " * (2047 - len(line)) + line + b"\n"
    refused = b" " + accepted
    stream_diagnostics([accepted, refused])
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.name == "listening_post.question_worker"
            ]
        )
        == 1
    )


@posix_only
def test_execute_requires_exact_result_verification(tmp_path):
    source = (
        "import json,sys\nvalue=json.load(sys.stdin)\nprint(json.dumps("
        + repr(execution_reply())
        + "))\n"
    )
    root = bundle(tmp_path, source)
    seen = []

    async def verify(receipt):
        seen.append(receipt)
        return True

    async def run():
        return await invoke_engine(
            root,
            Path(sys.executable),
            "execute",
            invocation(),
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
            verify_result=verify,
        )

    result = asyncio.run(run())
    assert result.reply == execution_reply()
    assert seen == [execution_reply()]
    assert result.stderr_observed_bytes == 0


@posix_only
def test_false_store_verification_cannot_acknowledge(tmp_path):
    root = bundle(
        tmp_path,
        "import json,sys\njson.load(sys.stdin)\nprint(json.dumps("
        + repr(execution_reply())
        + "))\n",
    )

    async def verify(_receipt):
        return False

    async def run():
        with pytest.raises(WorkerProcessError, match="worker_result_unverified"):
            await invoke_engine(
                root,
                Path(sys.executable),
                "execute",
                invocation(),
                deadline=time.monotonic() + 10,
                verify_runtime=lambda: None,
                verify_result=verify,
            )

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["stdout", "stderr", "timeout"])
@posix_only
def test_bounds_terminate_the_child_without_returning_its_text(tmp_path, mode):
    root = bundle(
        tmp_path,
        "import json,sys,time,os\njson.load(sys.stdin)\nopen('child.pid','w').write(str(os.getpid()))\n"
        + (
            "sys.stdout.write('x'*70000);sys.stdout.flush()\n"
            if mode == "stdout"
            else "sys.stderr.write('private'*50000);sys.stderr.flush()\n"
            if mode == "stderr"
            else ""
        )
        + "time.sleep(20)\n",
    )

    async def run():
        with pytest.raises(WorkerProcessError) as error:
            await invoke_engine(
                root,
                Path(sys.executable),
                "execute",
                invocation(),
                deadline=time.monotonic() + (0.3 if mode == "timeout" else 10),
                verify_runtime=lambda: None,
                verify_result=lambda _value: None,
            )
        assert "private" not in str(error.value)
        assert (
            error.value.reason
            == {
                "stdout": "worker_stdout_limit",
                "stderr": "worker_stderr_limit",
                "timeout": "worker_deadline_reached",
            }[mode]
        )

    asyncio.run(run())
    pid = int((root / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_expired_deadline_does_not_launch(tmp_path):
    root = bundle(tmp_path, "open('launched','w').write('bad')")

    async def run():
        with pytest.raises(WorkerProcessError, match="worker_deadline_reached"):
            await invoke_engine(
                root,
                Path(sys.executable),
                "execute",
                invocation(),
                deadline=time.monotonic() - 1,
                verify_runtime=lambda: None,
                verify_result=lambda _value: None,
            )

    asyncio.run(run())
    assert not (root / "launched").exists()


@posix_only
def test_worker_keeps_the_selected_virtual_environment(tmp_path):
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False).create(runtime)
    root = bundle(
        tmp_path / "engine",
        "import json,sys\njson.load(sys.stdin)\nassert sys.prefix == "
        + repr(str(runtime))
        + "\nprint(json.dumps("
        + repr(execution_reply())
        + "))\n",
    )

    async def verify(_receipt):
        return True

    async def run():
        return await invoke_engine(
            root,
            runtime / "bin/python",
            "execute",
            invocation(),
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
            verify_result=verify,
        )

    assert asyncio.run(run()).reply == execution_reply()


@posix_only
def test_cancellation_during_launch_waits_for_and_reaps_the_child(
    tmp_path, monkeypatch
):
    root = bundle(tmp_path, "import time\ntime.sleep(20)\n")
    native = asyncio.create_subprocess_exec
    processes = []

    async def delayed(*args, **kwargs):
        await asyncio.sleep(0.05)
        process = await native(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)

    async def run():
        task = asyncio.create_task(
            invoke_engine(
                root,
                Path(sys.executable),
                "execute",
                invocation(),
                deadline=time.monotonic() + 10,
                verify_runtime=lambda: None,
                verify_result=lambda _value: None,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(processes) == 1
        assert processes[0].returncode is not None

    asyncio.run(run())


@posix_only
def test_deadline_terminates_a_descendant_after_its_parent_exits(tmp_path):
    """The descendant is ready and its parent has exited before the deadline.

    The grandchild publishes its pid only once it ignores SIGTERM, and the parent
    waits for that before it exits, so a slow interpreter start on a loaded host
    can no longer end the case before the scenario it names exists.
    """
    grandchild = (
        "import os,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "open('grandchild.tmp','w').write(str(os.getpid()));"
        "os.replace('grandchild.tmp','grandchild.pid');time.sleep(20)"
    )
    root = bundle(
        tmp_path,
        "import json,os,subprocess,sys,time\njson.load(sys.stdin)\nsubprocess.Popen([sys.executable,'-c',"
        + repr(grandchild)
        + "])\nwhile not os.path.exists('grandchild.pid'):\n    time.sleep(.01)\n"
        "open('parent.exiting','w').close()\ntime.sleep(.1)\n",
    )

    async def run():
        with pytest.raises(WorkerProcessError, match="worker_deadline_reached"):
            await invoke_engine(
                root,
                Path(sys.executable),
                "execute",
                invocation(),
                deadline=time.monotonic() + 5,
                verify_runtime=lambda: None,
                verify_result=lambda _value: None,
            )

    asyncio.run(run())
    assert (root / "parent.exiting").is_file()
    pid = int((root / "grandchild.pid").read_text())
    state = Path(f"/proc/{pid}/stat")
    observed_state = None
    stop_at = time.monotonic() + 1
    while time.monotonic() < stop_at:
        try:
            observed_state = state.read_text().split()[2]
        except FileNotFoundError:
            return
        if observed_state == "Z":
            return
        time.sleep(0.01)
    pytest.fail(
        f"Descendant remains live after process-group termination: {observed_state}"
    )


@posix_only
@pytest.mark.parametrize("state", ["admitted", "running", "complete", "held"])
@pytest.mark.parametrize("accepted", [True, False])
def test_status_operation_verifies_terminal_results_but_not_pending(
    state, accepted, tmp_path
):
    reply = status_reply(state)
    root = bundle(
        tmp_path,
        "import json,sys\nassert sys.argv[1:] == ['--operation','status']\njson.load(sys.stdin)\nprint(json.dumps("
        + repr(reply)
        + "))\n",
    )
    seen = []

    async def verify(receipt):
        seen.append(receipt)
        return accepted

    async def run():
        return await invoke_engine(
            root,
            Path(sys.executable),
            "status",
            status_request(),
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
            verify_result=verify,
        )

    pending = state in {"admitted", "running"}
    if not pending and not accepted:
        with pytest.raises(WorkerProcessError, match="worker_result_unverified"):
            asyncio.run(run())
    else:
        assert asyncio.run(run()).reply == reply
    assert seen == ([] if pending else [reply])


@pytest.mark.parametrize(
    "code", ["snapshot_cap_version_unknown", "protected_context_registry_invalid"]
)
def test_snapshot_cap_refusal_survives_all_worker_diagnostic_filters(caplog, code):
    caplog.set_level(logging.INFO, logger="listening_post.question_worker")
    admission = {
        "contract_version": "general_question_admission_stage_v1",
        "request_id": invocation()["request_id"],
        "stage": "host_load",
        "state": "failed",
        "error_code": code,
        "elapsed_ms": 1,
        "read_attempts": None,
        "metadata_attempts": None,
        "body_attempts": None,
    }
    model = model_failure_record(error_code=code)
    drain = _WorkerDiagnosticDrain(invocation()["request_id"], "admit")
    for event in (admission, model, {"error": code}, {"error": "PRIVATE_DETAIL"}):
        drain.feed(json.dumps(event).encode() + b"\n")
    records = [
        json.loads(row.getMessage())
        for row in caplog.records
        if row.name == "listening_post.question_worker"
    ]
    assert len(records) == 3
    assert records[:2] == [admission, model]
    assert records[2]["code"] == code
    assert "PRIVATE_DETAIL" not in caplog.text


def _policy_freshness_event(stage="policy_freshness", error_code="approval_required"):
    return {
        "contract_version": "general_question_admission_stage_v1",
        "request_id": invocation()["request_id"],
        "stage": stage,
        "state": "failed",
        "error_code": error_code,
        "elapsed_ms": 12,
        "read_attempts": None,
        "metadata_attempts": None,
        "body_attempts": None,
    }


@pytest.mark.parametrize(
    ("operation", "lines", "expected"),
    [
        (
            "admit",
            [_policy_freshness_event(), {"error": "approval_required"}],
            True,
        ),
        # The terminal code alone does not say where the child stopped.
        ("admit", [{"error": "approval_required"}], False),
        # The stage alone does not say the child ended there.
        ("admit", [_policy_freshness_event()], False),
        (
            "admit",
            [_policy_freshness_event(), {"error": "storage_unavailable"}],
            False,
        ),
        (
            "admit",
            [
                _policy_freshness_event(stage="reservation"),
                {"error": "approval_required"},
            ],
            False,
        ),
        (
            "status",
            [_policy_freshness_event(), {"error": "approval_required"}],
            False,
        ),
    ],
)
def test_a_lapsed_review_is_named_only_when_the_child_stopped_before_writing(
    operation, lines, expected
):
    drain = _WorkerDiagnosticDrain(invocation()["request_id"], operation)
    for line in lines:
        drain.feed(json.dumps(line).encode() + b"\n")
    assert drain.policy_review_lapsed is expected


@pytest.mark.parametrize(
    ("stage_line", "expected"),
    [(True, "policy_review_lapsed"), (False, "worker_exit_failed")],
)
def test_actual_child_refused_at_policy_freshness_is_not_reported_uncertain(
    tmp_path, monkeypatch, stage_line, expected
):
    from src.api import question_worker_process as worker
    from tests.unit.test_question_worker_protocol import admission

    payload = admission()
    event = dict(_policy_freshness_event(), request_id=payload["request_id"])
    errors = (json.dumps(event) + "\n" if stage_line else "") + (
        '{"error":"approval_required"}\n'
    )
    root = bundle(
        tmp_path,
        "import sys,json\nvalue=json.load(sys.stdin)\nassert value == "
        + repr(payload)
        + "\nsys.stderr.write("
        + repr(errors)
        + ")\nsys.exit(1)\n",
    )
    native = asyncio.create_subprocess_exec

    async def portable_launch(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return await native(*args, **kwargs)

    async def portable_stop(process):
        if process.returncode is None:
            process.kill()
        await process.wait()

    monkeypatch.setattr(worker, "os", SimpleNamespace(name="posix", environ=os.environ))
    monkeypatch.setattr(worker, "_terminate_group", portable_stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", portable_launch)

    async def run():
        return await invoke_engine(
            root,
            Path(sys.executable),
            "admit",
            payload,
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
        )

    with pytest.raises(WorkerProcessError, match=expected):
        asyncio.run(run())
