import json
import re
from datetime import datetime
from uuid import UUID

STDIN_LIMIT = 256 * 1024
STDOUT_LIMIT = 64 * 1024
STDERR_LIMIT = 256 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[1-9][0-9]*\Z")
_INVOCATION_KEYS = {
    "contract_version",
    "request_id",
    "request_digest",
    "intake_digest",
    "policy_digest",
    "deployment_digest",
}
_ADMISSION_KEYS = {
    "contract_version",
    "request_id",
    "transport",
    "scope",
    "selected_market",
    "policy_digest",
    "deployment_digest",
}
_PARENT_KEYS = {"parent_request_id", "thread_anchor_request_id"}
# The optional client lens the request names. It is additive on both admission
# versions: the engine re-resolves it against the configuration bytes it holds,
# so the envelope asserts a lens and never grants one.
_LENS_KEYS = {"lens_binding_version", "client_lens_id", "configuration_digest"}
_LENS_BINDING_VERSION = "client_lens_binding_v1"
OBSERVATION_ERROR_CODES = frozenset(
    {"observation_request_unavailable", "observation_invalid"}
)
_OBSERVATION_MISSING = frozenset(
    {
        "execution_unconfirmed",
        "terminal_record_unavailable",
        "plan_record_unavailable",
        "record_invalid",
        "read_limit_reached",
        "read_deadline_reached",
    }
)
_OBSERVATION_STATES = frozenset(
    {
        "unconfirmed",
        "complete",
        "partial",
        "needs_clarification",
        "unavailable",
        "refused",
        "held",
    }
)


class WorkerProtocolError(ValueError):
    pass


def _exact(value, keys):
    return isinstance(value, dict) and set(value) == keys


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _lens_envelope(lens):
    """The three lens identity values, in the one shape both sides carry."""
    return (
        _exact(lens, _LENS_KEYS)
        and lens["lens_binding_version"] == _LENS_BINDING_VERSION
        and isinstance(lens["client_lens_id"], str)
        and bool(lens["client_lens_id"])
        and _digest(lens["configuration_digest"])
    )


def _parent_references(parent, anchor):
    return (parent is None and anchor is None) or (
        _uuid(parent) and (anchor is None or _uuid(anchor))
    )


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _generation(value):
    return isinstance(value, str) and _GENERATION.fullmatch(value) is not None


def _invocation(value):
    return (
        _exact(value, _INVOCATION_KEYS)
        and value["contract_version"] == "general_cultural_question_v1"
        and _uuid(value["request_id"])
        and all(
            _digest(value[key])
            for key in _INVOCATION_KEYS - {"request_id", "contract_version"}
        )
    )


def _scope(scope):
    if not _exact(
        scope,
        {
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
        },
    ):
        return False
    markets = scope["market_scope"]
    return (
        isinstance(scope["client_scope_id"], str)
        and bool(scope["client_scope_id"])
        and isinstance(markets, list)
        and bool(markets)
        and all(
            isinstance(market, str) and market in {"za", "ng", "ke"}
            for market in markets
        )
        and len(markets) == len(set(markets))
        and all(
            scope[key] is None or isinstance(scope[key], str)
            for key in ["brand_config_id", "theme_id"]
        )
        and isinstance(scope["audience_lens_ids"], list)
        and all(isinstance(lens, str) for lens in scope["audience_lens_ids"])
    )


def _admission(value):
    if not isinstance(value, dict):
        return False
    version = value.get("contract_version")
    if version == "general_question_admission_v1":
        keys = _ADMISSION_KEYS
    elif version == "general_question_admission_v2":
        keys = _ADMISSION_KEYS | _PARENT_KEYS
        if not _uuid(value.get("parent_request_id")) or not _parent_references(
            value.get("parent_request_id"), value.get("thread_anchor_request_id")
        ):
            return False
    else:
        return False
    lens = value.get("client_lens") if isinstance(value, dict) else None
    if lens is not None:
        keys = keys | {"client_lens"}
        if not _lens_envelope(lens):
            return False
    if not _exact(value, keys) or not _scope(value["scope"]):
        return False
    transport = value["transport"]
    return (
        _exact(transport, {"message", "history"})
        and _uuid(value["request_id"])
        and _digest(value["policy_digest"])
        and _digest(value["deployment_digest"])
        and isinstance(transport["message"], str)
        and isinstance(transport["history"], list)
        and (
            value["selected_market"] is None
            or value["selected_market"] in value["scope"]["market_scope"]
        )
    )


def _status(value):
    return (
        _exact(value, {"contract_version", "request_id", "scope"})
        and value["contract_version"] == "general_question_status_request_v1"
        and _uuid(value["request_id"])
        and _scope(value["scope"])
    )


def _observe(value):
    if not (
        _exact(value, {"contract_version", "request_id", "scope"})
        and value["contract_version"] == "general_question_observe_request_v1"
        and (value["request_id"] is None or _uuid(value["request_id"]))
    ):
        return False
    from src.api.fieldwork import FieldworkContractError, _scope_v2

    try:
        _scope_v2(value["scope"])
    except FieldworkContractError:
        return False
    return True


def encode_worker_input(operation, value):
    if not (
        (operation == "execute" and _invocation(value))
        or (operation == "admit" and _admission(value))
        or (operation == "status" and _status(value))
        or (operation == "observe" and _observe(value))
    ):
        raise WorkerProtocolError("worker_input_invalid")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkerProtocolError("worker_input_invalid") from None
    if len(encoded) > STDIN_LIMIT:
        raise WorkerProtocolError("worker_input_too_large")
    return encoded


def _read_reply(data, *, max_bytes=STDOUT_LIMIT):
    if not isinstance(data, bytes) or len(data) > max_bytes:
        raise WorkerProtocolError("worker_output_invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("nonfinite")

    try:
        result = json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite
        )
    except (ValueError, UnicodeError, RecursionError):
        raise WorkerProtocolError("worker_output_invalid") from None
    if not isinstance(result, dict):
        raise WorkerProtocolError("worker_output_invalid")
    return result


def decode_admission_reply(data, admitted_input):
    if not _admission(admitted_input):
        raise WorkerProtocolError("worker_input_invalid")
    value = _read_reply(data)
    keys = {
        "contract_version",
        "job_id",
        "invocation",
        "request_generation",
        "intake_generation",
        "deadline_at",
    }
    if (
        not _exact(value, keys)
        or value["contract_version"] != "general_question_admission_result_v1"
        or not _invocation(value["invocation"])
    ):
        raise WorkerProtocolError("worker_output_invalid")
    if value["job_id"] != "chat_" + UUID(admitted_input["request_id"]).hex:
        raise WorkerProtocolError("worker_output_identity_mismatch")
    if any(
        value["invocation"][key] != admitted_input[key]
        for key in ["request_id", "policy_digest", "deployment_digest"]
    ):
        raise WorkerProtocolError("worker_output_identity_mismatch")
    try:
        deadline = datetime.fromisoformat(value["deadline_at"])
        valid_deadline = (
            deadline.tzinfo is not None and deadline.utcoffset() is not None
        )
    except (TypeError, ValueError):
        valid_deadline = False
    if not valid_deadline or not all(
        _generation(value[key]) for key in ["request_generation", "intake_generation"]
    ):
        raise WorkerProtocolError("worker_output_invalid")
    return value


def decode_status_reply(data, status_input):
    if not _status(status_input):
        raise WorkerProtocolError("worker_input_invalid")
    value = _read_reply(data)
    status_keys = _INVOCATION_KEYS | {
        "state",
        "deadline_at",
        "result_digest",
        "result_generation",
    }
    if (
        not isinstance(value, dict)
        or not status_keys <= set(value) <= status_keys | {"client_lens"}
        or ("client_lens" in value and not _lens_envelope(value["client_lens"]))
        or value["contract_version"] != "general_question_status_v1"
        or not isinstance(value["state"], str)
        or value["state"]
        not in {
            "admitted",
            "running",
            "complete",
            "partial",
            "needs_clarification",
            "unavailable",
            "refused",
            "held",
        }
        or not all(
            _digest(value[key])
            for key in _INVOCATION_KEYS - {"request_id", "contract_version"}
        )
    ):
        raise WorkerProtocolError("worker_output_invalid")
    if value["request_id"] != status_input["request_id"]:
        raise WorkerProtocolError("worker_output_identity_mismatch")
    try:
        deadline = datetime.fromisoformat(value["deadline_at"])
        valid_deadline = (
            deadline.tzinfo is not None and deadline.utcoffset() is not None
        )
    except (TypeError, ValueError):
        valid_deadline = False
    if not valid_deadline:
        raise WorkerProtocolError("worker_output_invalid")
    if value["state"] in {"admitted", "running"}:
        valid_pointer = (
            value["result_digest"] is None and value["result_generation"] is None
        )
    else:
        valid_pointer = _digest(value["result_digest"]) and _generation(
            value["result_generation"]
        )
    if not valid_pointer:
        raise WorkerProtocolError("worker_output_invalid")
    return value


def decode_execution_reply(data, invocation):
    if not _invocation(invocation):
        raise WorkerProtocolError("worker_input_invalid")
    value = _read_reply(data)
    if not _exact(
        value,
        {
            "request_id",
            "request_digest",
            "intake_digest",
            "result_digest",
            "result_generation",
            "state",
        },
    ):
        raise WorkerProtocolError("worker_output_invalid")
    if any(
        value[key] != invocation[key]
        for key in ["request_id", "request_digest", "intake_digest"]
    ):
        raise WorkerProtocolError("worker_output_identity_mismatch")
    if (
        not _digest(value["result_digest"])
        or not _generation(value["result_generation"])
        or not isinstance(value["state"], str)
        or value["state"] not in {"terminal", "held"}
    ):
        raise WorkerProtocolError("worker_output_invalid")
    return value


def decode_observe_reply(data, observe_input):
    if not _observe(observe_input):
        raise WorkerProtocolError("worker_input_invalid")
    value = _read_reply(data)
    from src.api import fieldwork

    try:
        if value.get("contract_version") != "general_question_observe_reply_v1":
            raise ValueError()
        if observe_input["request_id"] is None:
            if (
                not _exact(
                    value,
                    {
                        "contract_version",
                        "mode",
                        "ledger_generation",
                        "coverage",
                        "rows",
                    },
                )
                or value["mode"] != "inventory"
            ):
                raise ValueError()
            fieldwork._coverage_v2(value["coverage"], "general_questions")
            generation = value["ledger_generation"]
            if not _generation(generation) and not (
                generation is None and value["coverage"]["state"] == "unavailable"
            ):
                raise ValueError()
            rows = value["rows"]
            if not isinstance(rows, list) or len(rows) > fieldwork.RESEARCH_ROW_LIMIT:
                raise ValueError()
            fieldwork.build_fieldwork_workspace_v2(
                scope=observe_input["scope"],
                general_questions={
                    "scope": observe_input["scope"],
                    "coverage": value["coverage"],
                    "rows": rows,
                },
                investigation_plans=None,
            )
            if rows != sorted(rows, key=fieldwork._row_sort_v2):
                raise ValueError()
        else:
            if (
                not _exact(
                    value,
                    {
                        "contract_version",
                        "mode",
                        "ledger_generation",
                        "invocation",
                        "request_generation",
                        "intake_generation",
                        "observed_state",
                        "result_pointer",
                        "reserved_microusd",
                        "missing_work",
                    },
                )
                or value["mode"] != "detail"
            ):
                raise ValueError()
            invocation = value["invocation"]
            state = value["observed_state"]
            if (
                not _invocation(invocation)
                or invocation["request_id"] != observe_input["request_id"]
                or not all(
                    _generation(value[key])
                    for key in (
                        "ledger_generation",
                        "request_generation",
                        "intake_generation",
                    )
                )
                or not isinstance(state, str)
                or state not in _OBSERVATION_STATES
                or type(value["reserved_microusd"]) is not int
                or value["reserved_microusd"] < 0
            ):
                raise ValueError()
            missing = value["missing_work"]
            if (
                not isinstance(missing, list)
                or not all(
                    isinstance(code, str) and code in _OBSERVATION_MISSING
                    for code in missing
                )
                or len(missing) != len(set(missing))
            ):
                raise ValueError()
            if state == "unconfirmed":
                if value["result_pointer"] is not None:
                    raise ValueError()
            else:
                original_pointer = value["result_pointer"]
                if (
                    not isinstance(original_pointer, dict)
                    or original_pointer.get("state") != state
                ):
                    raise ValueError()
                pointer = decode_execution_reply(
                    json.dumps(
                        {
                            **original_pointer,
                            "state": "held" if state == "held" else "terminal",
                        },
                        allow_nan=False,
                    ).encode(),
                    invocation,
                )
                if (state == "held") != (pointer["state"] == "held"):
                    raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise WorkerProtocolError("worker_output_invalid") from None
    return value
