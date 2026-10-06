import asyncio
import hashlib
import json
import keyword
import logging
import math
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from src.api.question_worker_protocol import (
    OBSERVATION_ERROR_CODES,
    decode_observe_reply,
    STDERR_LIMIT,
    STDOUT_LIMIT,
    _read_reply,
    _uuid,
    decode_admission_reply,
    decode_execution_reply,
    decode_status_reply,
    encode_worker_input,
)

_DIAGNOSTIC_LOGGER = logging.getLogger("listening_post.question_worker")
_DIAGNOSTIC_LOGGER.setLevel(logging.INFO)
_DIAGNOSTIC_CONFIG_LOCK = threading.Lock()


class _WorkerDiagnosticStreamHandler(logging.StreamHandler):
    _question_worker_console = True

    def emit(self, record):
        try:
            if record.exc_info or len(self.format(record).encode("utf-8")) > 2048:
                return
            super().emit(record)
        except Exception:
            pass

    def handleError(self, record):
        pass


def configure_worker_diagnostic_logging():
    """Install one bounded message-only console sink while retaining capture propagation."""
    with _DIAGNOSTIC_CONFIG_LOCK:
        if not any(
            getattr(handler, "_question_worker_console", False)
            for handler in _DIAGNOSTIC_LOGGER.handlers
        ):
            handler = _WorkerDiagnosticStreamHandler()
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter("%(message)s"))
            _DIAGNOSTIC_LOGGER.addHandler(handler)


_DIAGNOSTIC_FIELDS = {
    "contract_version",
    "request_id",
    "phase",
    "state",
    "code",
    "elapsed_ms",
}
_DIAGNOSTIC_PHASES = {"execution", "planning", "retrieval", "answering", "result"}
_ADMISSION_STAGE_FIELDS = {
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
_ADMISSION_STAGES = {
    "host_load",
    "admission",
    "authority",
    "child_lookup",
    "policy_freshness",
    "intake_context",
    "parent_intake",
    "context_prefetch",
    "scope_validation",
    "result_prefetch",
    "parent_validation",
    "anchor_validation",
    "capsule_write",
    "reservation",
}
_ADMISSION_STAGE_ERRORS = {
    "admission_failed",
    "parent_unavailable",
    "parent_context_invalid",
    "parent_context_conflict",
    "parent_read_budget_exhausted",
    "parent_context_deadline",
    "parent_source_unavailable",
    "parent_alias_invalid",
    "parent_reference_invalid",
    "scope_invalid",
    "control_invalid",
    "protected_context_registry_invalid",
    "snapshot_cap_version_unknown",
    "request_invalid",
    "run_id_conflict",
    "approval_required",
    "storage_unavailable",
    "object_version_unavailable",
    "budget_exhausted",
    "request_expired",
    "control_conflict",
}
_DIAGNOSTIC_STATES = {"started", "succeeded", "failed", "held", "reused"}
_DIAGNOSTIC_CODES = {
    "request_invalid",
    "scope_invalid",
    "approval_required",
    "request_expired",
    "retrieval_incomplete",
    "evidence_insufficient",
    "claim_support_failed",
    "semantic_output_invalid",
    "model_timeout",
    "metering_persistence_failed",
    "response_usage_unavailable",
    "storage_unavailable",
    "worker_failed",
    "budget_exhausted_no_grounded_result",
}
_MODEL_FAILURE_FIELDS = {
    "contract_version",
    "request_id",
    "stage",
    "exception_class",
    "error_code",
    "provider_status",
    "provider_status_label",
    "provider_message",
}
_MODEL_FAILURE_CODES = {
    "call_invalid",
    "response_invalid",
    "response_unknown",
    "response_model_invalid",
    "usage_unknown",
    "usage_unresolved",
    "metering_failed",
    "count_invalid",
    "budget_exhausted",
    "call_already_started",
    "request_expired",
    "request_terminal",
    "control_conflict",
    "control_invalid",
    "protected_context_registry_invalid",
    "snapshot_cap_version_unknown",
    "storage_unavailable",
    "storage_conflict",
    "approval_required",
    "model_timeout",
    "model_unavailable",
}
_RETRIEVAL_CAUSE_FIELDS = {
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
# The engine's own reason code grammar; builder text that is not a code never reaches
# the log, and the engine already drops it before emitting.
_RETRIEVAL_CAUSE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# The shape of type(error).__name__ for every exception class the engine defines,
# imports or inherits from builtins; checked against the engine tree before pinning.
_EXCEPTION_CLASS_NAME = re.compile(r"_*[A-Z][A-Za-z0-9]*\Z")
_PARENT_CONTEXT_CODES = {
    "parent_reference_invalid",
    "parent_unavailable",
    "parent_scope_mismatch",
    "parent_context_invalid",
    "parent_context_conflict",
    "parent_read_budget_exhausted",
    "parent_context_deadline",
    "parent_source_unavailable",
    "parent_alias_invalid",
}
_EARLY_CHILD_CODES = _PARENT_CONTEXT_CODES | {
    "operation_invalid",
    "request_invalid",
    "request_expired",
    "request_unknown",
    "scope_invalid",
    "run_id_conflict",
    "approval_required",
    "budget_exhausted",
    "control_invalid",
    "protected_context_registry_invalid",
    "snapshot_cap_version_unknown",
    "control_unavailable",
    "control_conflict",
    "storage_unavailable",
    "storage_conflict",
    "identity_invalid",
    "host_environment_invalid",
    "host_unavailable",
    "bundle_invalid",
    "runtime_build_invalid",
    "host_deadline_exceeded",
    "result_invalid",
    "result_unavailable",
    "result_support_unverified",
    "request_terminal",
    "worker_failed",
}
_PARENT_OPERATIONS = {
    "configuration",
    "admission",
    "status",
    "authentication",
    "task_validation",
    "binding_validation",
    "request_read",
    "scope_validation",
    "execution",
    "queue_dispatch",
    "queue_create",
    "queue_readback",
}
_PARENT_CODES = _EARLY_CHILD_CODES | {
    "policy_review_lapsed",
    "operation_started",
    "operation_succeeded",
    "worker_unconfigured",
    "worker_cancelled",
    "worker_task_invalid",
    "worker_binding_invalid",
    "worker_scope_invalid",
    "worker_command_invalid",
    "worker_command_unavailable",
    "worker_deadline_invalid",
    "worker_deadline_reached",
    "worker_exit_failed",
    "worker_io_failed",
    "worker_launch_failed",
    "worker_platform_unavailable",
    "worker_result_unverified",
    "worker_result_verifier_missing",
    "worker_status_expired",
    "worker_stderr_limit",
    "worker_stdout_limit",
    "worker_input_invalid",
    "worker_input_too_large",
    "worker_output_identity_mismatch",
    "worker_output_invalid",
    "result_digest_mismatch",
    "result_encoding_invalid",
    "result_envelope_invalid",
    "result_evidence_binding_invalid",
    "result_generation_mismatch",
    "result_identity_mismatch",
    "result_response_invalid",
    "result_response_identity_mismatch",
    "result_response_state_mismatch",
    "result_scope_mismatch",
    "result_status_mismatch",
    "result_state_invalid",
    "result_time_invalid",
    "result_pointer_invalid",
    "result_size_invalid",
    "result_size_mismatch",
    "result_store_unavailable",
    "result_deadline_invalid",
    "result_deadline_reached",
    "worker_oidc_invalid",
    "worker_oidc_forbidden",
    "worker_oidc_unavailable",
    "queue_create_acknowledged",
    "queue_create_conflict",
    "queue_create_timeout",
    "queue_create_transport_unavailable",
    "queue_create_redirect_refused",
    "queue_create_http_rejected",
    "queue_readback_verified",
    "queue_readback_missing",
    "queue_readback_http_rejected",
    "queue_readback_redirect_refused",
    "queue_readback_timeout",
    "queue_readback_transport_unavailable",
    "queue_readback_invalid",
    "queue_readback_mismatch",
    "queue_deadline_reached",
}


def parent_failure_code(error, fallback="worker_failed"):
    if isinstance(error, asyncio.CancelledError):
        return "worker_cancelled"
    try:
        values = (
            getattr(error, "code", None),
            getattr(error, "reason", None),
            error.args[0] if error.args else None,
        )
    except Exception:
        return "worker_failed"
    for value in values:
        if type(value) is str and value in _PARENT_CODES:
            return value
    return fallback if fallback in _PARENT_CODES else "worker_failed"


def emit_parent_diagnostic(
    operation, state, code, *, request_id=None, http_status=None
):
    """Emit bounded parent-owned context, never arbitrary exception or provider text."""
    if operation not in _PARENT_OPERATIONS or state not in {
        "started",
        "succeeded",
        "failed",
        "unresolved",
        "reused",
    }:
        return
    if type(request_id) is not str or len(request_id) != 36 or not _uuid(request_id):
        request_id = None
    if type(http_status) is not int or not 100 <= http_status <= 599:
        http_status = None
    record = {
        "contract_version": "general_question_parent_diagnostic_v1",
        "request_id": request_id,
        "operation": operation,
        "state": state,
        "code": code
        if type(code) is str and code in _PARENT_CODES
        else "worker_failed",
        "http_status": http_status,
    }
    try:
        _DIAGNOSTIC_LOGGER.log(
            logging.WARNING if state in {"failed", "unresolved"} else logging.INFO,
            json.dumps(record, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        pass


def _retrieval_code(value):
    return type(value) is str and _RETRIEVAL_CAUSE_CODE.fullmatch(value) is not None


class _WorkerDiagnosticDrain:
    def __init__(self, request_id, operation="execute"):
        self.request_id = request_id
        self.operation = {
            "admit": "admission",
            "status": "status",
            "execute": "execution",
            "observe": "observation",
        }.get(operation, "execution")
        self.pending = bytearray()
        self.discarding = False
        self._parent_errors = set()
        self._observation_errors = set()
        self._admission_errors = set()
        self._policy_freshness_refused = False

    @property
    def observation_error(self):
        return (
            next(iter(self._observation_errors))
            if len(self._observation_errors) == 1
            else None
        )

    @property
    def parent_error(self):
        return (
            next(iter(self._parent_errors)) if len(self._parent_errors) == 1 else None
        )

    @property
    def policy_review_lapsed(self):
        """The admit child stopped at the policy check, before it wrote anything.

        Both halves are required: the stage event says where the child stopped,
        and the terminal error line says that is where it ended. Either alone
        leaves the outcome as uncertain as any other exit.
        """
        return (
            self.operation == "admission"
            and self._policy_freshness_refused
            and self._admission_errors == {"approval_required"}
        )

    def feed(self, chunk):
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            if not self.discarding:
                if len(self.pending) + end - offset > 2047:
                    self.pending.clear()
                    self.discarding = True
                else:
                    self.pending.extend(chunk[offset:end])
            if newline < 0:
                return
            if not self.discarding:
                self._emit(bytes(self.pending))
            self.pending.clear()
            self.discarding = False
            offset = newline + 1

    def _emit(self, line):
        try:
            value = _read_reply(line, max_bytes=2048)
        except ValueError:
            return
        if set(value) == _ADMISSION_STAGE_FIELDS:
            counts = [
                value[key]
                for key in ("read_attempts", "metadata_attempts", "body_attempts")
            ]
            valid_counts = all(item is None for item in counts) or (
                all(type(item) is int and 0 <= item <= 40 for item in counts)
                and counts[0] == counts[1]
                and counts[2] <= counts[1]
            )
            if (
                self.operation != "admission"
                or value["contract_version"] != "general_question_admission_stage_v1"
                or value["request_id"] != self.request_id
                or type(value["request_id"]) is not str
                or not _uuid(value["request_id"])
                or type(value["stage"]) is not str
                or value["stage"] not in _ADMISSION_STAGES
                or type(value["state"]) is not str
                or value["state"] not in {"started", "succeeded", "failed"}
                or type(value["elapsed_ms"]) is not int
                or not 0 <= value["elapsed_ms"] <= 60000
                or not valid_counts
                or (
                    value["error_code"] is not None
                    and (
                        type(value["error_code"]) is not str
                        or value["error_code"] not in _ADMISSION_STAGE_ERRORS
                    )
                )
                or (value["state"] == "failed") != (value["error_code"] is not None)
            ):
                return
            if (
                value["stage"] == "policy_freshness"
                and value["state"] == "failed"
                and value["error_code"] == "approval_required"
            ):
                self._policy_freshness_refused = True
            try:
                _DIAGNOSTIC_LOGGER.log(
                    logging.WARNING if value["state"] == "failed" else logging.INFO,
                    json.dumps(value, sort_keys=True, separators=(",", ":")),
                )
            except Exception:
                pass
            return
        if set(value) == {"error"}:
            if (
                self.operation == "observation"
                and type(value["error"]) is str
                and value["error"] in OBSERVATION_ERROR_CODES
            ):
                self._observation_errors.add(value["error"])
                return
            if self.operation == "admission":
                self._admission_errors.add(
                    value["error"] if type(value["error"]) is str else None
                )
            if type(value["error"]) is str and value["error"] in _EARLY_CHILD_CODES:
                if (
                    self.operation == "admission"
                    and value["error"] in _PARENT_CONTEXT_CODES
                ):
                    self._parent_errors.add(value["error"])
                emit_parent_diagnostic(
                    self.operation, "failed", value["error"], request_id=self.request_id
                )
            return
        if set(value) == _MODEL_FAILURE_FIELDS:
            status = value["provider_status"]
            label = value["provider_status_label"]
            message = value["provider_message"]
            if (
                value["contract_version"] != "general_question_model_failure_v1"
                or value["request_id"] != self.request_id
                or type(value["stage"]) is not str
                or value["stage"] not in {"planning", "answering"}
                or type(value["exception_class"]) is not str
                or not 1 <= len(value["exception_class"]) <= 100
                or (
                    value["error_code"] is not None
                    and (
                        type(value["error_code"]) is not str
                        or value["error_code"] not in _MODEL_FAILURE_CODES
                    )
                )
                or (
                    status is not None
                    and (type(status) is not int or not 100 <= status <= 599)
                )
                or (
                    label is not None
                    and (
                        type(label) is not str
                        or not 1 <= len(label) <= 64
                        or any(
                            character != "_" and not "A" <= character <= "Z"
                            for character in label
                        )
                    )
                )
                or (
                    message is not None
                    and (
                        type(message) is not str or len(message.encode("utf-8")) > 1000
                    )
                )
            ):
                return
            record = {field: value[field] for field in _MODEL_FAILURE_FIELDS}
            try:
                _DIAGNOSTIC_LOGGER.warning(
                    json.dumps(
                        record,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                )
            except Exception:
                pass
            return
        if set(value) == _RETRIEVAL_CAUSE_FIELDS:
            missing = value["builder_missing_work"]
            omitted = value["builder_missing_work_omitted"]
            exception_type = value["exception_type"]
            # The engine emits this shape only from the retrieval phase of an execute
            # child, and exception_type is type(error).__name__ and nothing else, so
            # anything not shaped like a Python exception class name (optional leading
            # underscores, an upper case ASCII letter, then ASCII letters and digits,
            # and not a keyword) is free text and never logged.
            if (
                self.operation != "execution"
                or value["contract_version"] != "general_question_retrieval_cause_v1"
                or value["request_id"] != self.request_id
                or type(value["request_id"]) is not str
                or not _uuid(value["request_id"])
                or value["phase"] != "retrieval"
                or type(value["public_code"]) is not str
                or value["public_code"] not in _DIAGNOSTIC_CODES
                or type(exception_type) is not str
                or not 1 <= len(exception_type) <= 100
                or _EXCEPTION_CLASS_NAME.fullmatch(exception_type) is None
                or keyword.iskeyword(exception_type)
                or any(
                    value[field] is not None and not _retrieval_code(value[field])
                    for field in ("exception_code", "builder_status", "builder_reason")
                )
                or (
                    missing is not None
                    and (
                        type(missing) is not list
                        or len(missing) > 8
                        or not all(_retrieval_code(item) for item in missing)
                    )
                )
                or type(omitted) is not int
                or not 0 <= omitted <= 1000
                or type(value["elapsed_ms"]) is not int
                or not 0 <= value["elapsed_ms"] <= 210000
            ):
                return
            record = {field: value[field] for field in _RETRIEVAL_CAUSE_FIELDS}
            try:
                _DIAGNOSTIC_LOGGER.warning(
                    json.dumps(record, sort_keys=True, separators=(",", ":"))
                )
            except Exception:
                pass
            return
        if (
            set(value) != _DIAGNOSTIC_FIELDS
            or value["contract_version"] != "general_question_diagnostic_v1"
            or value["request_id"] != self.request_id
            or type(value["phase"]) is not str
            or value["phase"] not in _DIAGNOSTIC_PHASES
            or type(value["state"]) is not str
            or value["state"] not in _DIAGNOSTIC_STATES
            or (
                value["code"] is not None
                and (
                    type(value["code"]) is not str
                    or value["code"] not in _DIAGNOSTIC_CODES
                )
            )
            or type(value["elapsed_ms"]) is not int
            or not 0 <= value["elapsed_ms"] <= 210000
        ):
            return
        record = {field: value[field] for field in _DIAGNOSTIC_FIELDS}
        try:
            _DIAGNOSTIC_LOGGER.info(
                json.dumps(record, sort_keys=True, separators=(",", ":"))
            )
        except Exception:
            pass


class WorkerProcessError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class EngineReply:
    reply: dict
    stderr_observed_bytes: int
    stderr_observed_sha256: str


async def _terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=5)
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def invoke_engine(
    bundle_root: Path,
    interpreter: Path,
    operation: str,
    payload: dict,
    *,
    deadline: float,
    verify_runtime,
    verify_result=None,
    environment=None,
):
    encoded = encode_worker_input(operation, payload)
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise WorkerProcessError("worker_deadline_invalid")
    if time.monotonic() >= deadline:
        raise WorkerProcessError("worker_deadline_reached")
    if operation in {"execute", "status"} and verify_result is None:
        raise WorkerProcessError("worker_result_verifier_missing")
    verify_runtime()
    if time.monotonic() >= deadline:
        raise WorkerProcessError("worker_deadline_reached")
    if os.name != "posix":
        raise WorkerProcessError("worker_platform_unavailable")
    if not bundle_root.is_absolute() or not interpreter.is_absolute():
        raise WorkerProcessError("worker_command_invalid")
    try:
        root = bundle_root.resolve(strict=True)
        entry = (root / "scripts/staging/run_general_question_worker.py").resolve(
            strict=True
        )
    except (OSError, RuntimeError):
        raise WorkerProcessError("worker_command_unavailable") from None
    executable = interpreter
    if (
        not entry.is_relative_to(root)
        or not entry.is_file()
        or not executable.is_file()
    ):
        raise WorkerProcessError("worker_command_invalid")
    env = dict(os.environ if environment is None else environment)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    process = None
    launch = None
    completed = None
    tasks = []
    stderr_bytes = 0
    stderr_hash = hashlib.sha256()
    diagnostics = _WorkerDiagnosticDrain(payload["request_id"], operation)
    fault = asyncio.get_running_loop().create_future()

    async def read_pipe(stream, limit, reason, capture):
        nonlocal stderr_bytes
        kept = bytearray()
        count = 0
        while chunk := await stream.read(4096):
            count += len(chunk)
            if reason == "worker_stderr_limit":
                stderr_bytes += len(chunk)
                stderr_hash.update(chunk)
                if count <= limit:
                    diagnostics.feed(chunk)
            if count > limit:
                if not fault.done():
                    fault.set_result(reason)
                await asyncio.sleep(0)
            elif capture:
                kept.extend(chunk)
        return bytes(kept)

    async def write_input():
        process.stdin.write(encoded)
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    async def cleanup():
        nonlocal process
        if process is None and launch is not None:
            try:
                process = await launch
            except (OSError, WorkerProcessError):
                pass
        if process is not None:
            await _terminate_group(process)
            # Process exit does not close every pipe before the event loop stops.
            process._transport.close()
            await asyncio.sleep(0)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if completed is not None:
            await asyncio.gather(completed, return_exceptions=True)

    async def launch_child():
        if time.monotonic() >= deadline:
            raise WorkerProcessError("worker_deadline_reached")
        return await asyncio.create_subprocess_exec(
            str(executable),
            str(entry),
            "--operation",
            operation,
            cwd=str(root),
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    try:
        async with asyncio.timeout_at(deadline):
            launch = asyncio.create_task(launch_child())
            try:
                process = await asyncio.shield(launch)
            except OSError:
                raise WorkerProcessError("worker_launch_failed") from None
            tasks = [
                asyncio.create_task(write_input()),
                asyncio.create_task(
                    read_pipe(process.stdout, STDOUT_LIMIT, "worker_stdout_limit", True)
                ),
                asyncio.create_task(
                    read_pipe(
                        process.stderr, STDERR_LIMIT, "worker_stderr_limit", False
                    )
                ),
                asyncio.create_task(process.wait()),
            ]
            completed = asyncio.gather(*tasks)
            await asyncio.wait([completed, fault], return_when=asyncio.FIRST_COMPLETED)
            if fault.done():
                raise WorkerProcessError(fault.result())
            try:
                _, stdout, _, exit_code = await completed
            except (BrokenPipeError, ConnectionError, OSError):
                raise WorkerProcessError("worker_io_failed") from None
            if exit_code != 0:
                if operation == "admit" and diagnostics.policy_review_lapsed:
                    raise WorkerProcessError("policy_review_lapsed")
                if operation == "observe" and diagnostics.observation_error is not None:
                    raise WorkerProcessError(diagnostics.observation_error)
                if (
                    operation == "admit"
                    and payload["contract_version"] == "general_question_admission_v2"
                    and diagnostics.parent_error is not None
                ):
                    raise WorkerProcessError(diagnostics.parent_error)
                raise WorkerProcessError("worker_exit_failed")
            if operation == "admit":
                reply = decode_admission_reply(stdout, payload)
            elif operation == "status":
                reply = decode_status_reply(stdout, payload)
            elif operation == "observe":
                reply = decode_observe_reply(stdout, payload)
            else:
                reply = decode_execution_reply(stdout, payload)
            needs_result = operation == "execute" or (
                operation == "status" and reply["result_digest"] is not None
            )
            if needs_result and await verify_result(reply) is not True:
                raise WorkerProcessError("worker_result_unverified")
            return EngineReply(reply, stderr_bytes, stderr_hash.hexdigest())
    except TimeoutError:
        raise WorkerProcessError("worker_deadline_reached") from None
    finally:
        stop = asyncio.create_task(cleanup())
        cancelled = False
        while not stop.done():
            try:
                await asyncio.shield(stop)
            except asyncio.CancelledError:
                cancelled = True
        await stop
        if not fault.done():
            fault.cancel()
        if cancelled:
            raise asyncio.CancelledError
