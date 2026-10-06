"""Bounded process entrypoint for the implemented general-question operations."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_INPUT_BYTES = 256 * 1024
_OUTPUT_BYTES = 64 * 1024
_ERRORS = {
    "observation_request_unavailable",
    "observation_invalid",
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
_ADMISSION_FIELDS = {
    "contract_version",
    "request_id",
    "transport",
    "scope",
    "selected_market",
    "policy_digest",
    "deployment_digest",
}


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("request_invalid")
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("request_invalid")


def _input(stream, operation):
    raw = stream.read(_INPUT_BYTES + 1)
    if type(raw) is not bytes or not raw or len(raw) > _INPUT_BYTES:
        raise ValueError("request_invalid")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    fields, version = {
        "admit": (_ADMISSION_FIELDS, "general_question_admission_v1"),
        "status": (
            {"contract_version", "request_id", "scope"},
            "general_question_status_request_v1",
        ),
        "observe": (
            {"contract_version", "request_id", "scope"},
            "general_question_observe_request_v1",
        ),
        "execute": (
            {
                "contract_version",
                "request_id",
                "request_digest",
                "intake_digest",
                "policy_digest",
                "deployment_digest",
            },
            "general_cultural_question_v1",
        ),
    }[operation]
    if (
        operation == "admit"
        and type(value) is dict
        and value.get("contract_version") == "general_question_admission_v2"
    ):
        fields = _ADMISSION_FIELDS | {"parent_request_id", "thread_anchor_request_id"}
        version = "general_question_admission_v2"
    if type(value) is not dict or set(value) != fields or value["contract_version"] != version:
        raise ValueError("request_invalid")
    if operation == "observe":
        from src.analysis.open_intelligence.general_question_observation import (
            validate_observe_request,
        )

        value = validate_observe_request(value)
    return value


def main(argv=None, *, stdin=None, stdout=None, stderr=None, host_factory=None, now=None):
    args = sys.argv[1:] if argv is None else argv
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout.buffer if stdout is None else stdout
    error_stream = sys.stderr.buffer if stderr is None else stderr

    def diagnostic(event):
        error_stream.write(_json_bytes(event) + b"\n")
        error_stream.flush()

    if args not in (
        ["--operation", "admit"],
        ["--operation", "status"],
        ["--operation", "execute"],
        ["--operation", "observe"],
    ):
        error_stream.write(_json_bytes({"error": "operation_invalid"}) + b"\n")
        error_stream.flush()
        return 1
    try:
        value = _input(input_stream, args[1])
    except (ValueError, TypeError, RecursionError):
        error_stream.write(_json_bytes({"error": "request_invalid"}) + b"\n")
        error_stream.flush()
        return 1
    try:
        from src.analysis.open_intelligence.general_question_admission import (
            admit_question_transport,
        )

        if host_factory is None:
            from src.analysis.open_intelligence.general_question_host import load_question_host

            host_factory = load_question_host
        from src.analysis.open_intelligence.general_question_parent_context import admission_stage

        with admission_stage(
            "host_load",
            request_id=value["request_id"],
            diagnostics=diagnostic if args[1] == "admit" else None,
        ):
            host = host_factory(engine_root=_ROOT)
        timestamp = (datetime.now(UTC) if now is None else now()) if args[1] != "execute" else None
        if args[1] == "admit":
            result = admit_question_transport(
                value,
                store=host["store"],
                runtime_identity=host["runtime_identity"],
                now=timestamp,
                diagnostics=diagnostic,
            )
        elif args[1] == "observe":
            from src.analysis.open_intelligence.general_question_observation import (
                observe_question_requests,
            )

            result = observe_question_requests(value, store=host["store"], now=timestamp)
        elif args[1] == "status":
            from src.analysis.open_intelligence.general_question_execution import (
                read_general_question_status,
            )

            result = read_general_question_status(
                value["request_id"], store=host["store"], scope=value["scope"], now=timestamp
            )
        else:
            import asyncio

            from src.analysis.open_intelligence.general_question_context_admission import (
                _ISOLATED_READER_EXECUTION,
            )
            from src.analysis.open_intelligence.general_question_execution import (
                execute_general_question,
            )

            async def execute_at_start():
                token = _ISOLATED_READER_EXECUTION.set(True)
                try:
                    timestamp = datetime.now(UTC) if now is None else now()
                    return await execute_general_question(
                        value,
                        store=host["store"],
                        runtime_identity=host["runtime_identity"],
                        credentials=host["credentials"],
                        now=timestamp,
                        diagnostics=diagnostic,
                    )
                finally:
                    _ISOLATED_READER_EXECUTION.reset(token)

            result = asyncio.run(execute_at_start())
            if result.get("state") not in {
                "complete",
                "partial",
                "needs_clarification",
                "unavailable",
                "refused",
                "held",
            }:
                raise ValueError("output_invalid")
            result = {**result, "state": "held" if result["state"] == "held" else "terminal"}
        raw = _json_bytes(result) + b"\n"
        if len(raw) > _OUTPUT_BYTES:
            raise ValueError("output_invalid")
        output_stream.write(raw)
        output_stream.flush()
        return 0
    except Exception as error:
        code = getattr(error, "code", None)
        reason = code if type(code) is str and code in _ERRORS else "worker_failed"
        error_stream.write(_json_bytes({"error": reason}) + b"\n")
        error_stream.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
