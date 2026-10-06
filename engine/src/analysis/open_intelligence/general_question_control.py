"""Validation of the fixed evaluation allowance and immutable admissions."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_policy import validate_question_policy

ALLOWANCE_ID = "general_cultural_question_staging_eval_v1"
REQUEST_MICROUSD = 100000
MAX_REQUESTS = 100
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {
    "contract_version",
    "allowance_id",
    "active_deployment_digest",
    "bindings",
    "requests",
    "reserved_microusd",
}
_ADMISSION_FIELDS = {
    "request_digest",
    "intake_digest",
    "request_generation",
    "intake_generation",
    "client_scope_id",
    "policy_digest",
    "deployment_digest",
    "admitted_at",
    "deadline_at",
    "reserved_microusd",
}


class QuestionStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_digest(value: object, code: str = "control_invalid") -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise QuestionStoreError(code)
    return value


def require_request_id(value: object) -> str:
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise QuestionStoreError("request_invalid")
    return value


def control_time(value: object) -> datetime:
    try:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != timedelta(0):
            raise ValueError()
        return parsed
    except (ValueError, TypeError, OverflowError) as exc:
        raise QuestionStoreError("control_invalid") from exc


def utc_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise QuestionStoreError("request_invalid")
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise QuestionStoreError("request_invalid") from exc


def build_initial_question_control(policy: dict, *, deployment_digest: str) -> dict:
    """Prepare activation bytes only; the runtime cannot create this ledger."""
    policy = validate_question_policy(policy)
    require_digest(deployment_digest)
    return {
        "contract_version": "general_question_allowance_v1",
        "allowance_id": ALLOWANCE_ID,
        "active_deployment_digest": deployment_digest,
        "bindings": {deployment_digest: policy["policy_digest"]},
        "requests": {},
        "reserved_microusd": 0,
    }


def validate_question_control(value: object, *, policy_resolver=None) -> dict:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise QuestionStoreError("control_invalid")
    if (
        value["contract_version"] != "general_question_allowance_v1"
        or value["allowance_id"] != ALLOWANCE_ID
    ):
        raise QuestionStoreError("control_invalid")
    bindings = value["bindings"]
    requests = value["requests"]
    if not isinstance(bindings, dict) or not bindings or not isinstance(requests, dict):
        raise QuestionStoreError("control_invalid")
    for deployment, policy in bindings.items():
        require_digest(deployment)
        require_digest(policy)
    if not callable(policy_resolver):
        raise QuestionStoreError("control_invalid")
    policies = {}
    for policy_digest in set(bindings.values()):
        try:
            policy = policy_resolver(policy_digest)
            if policy is None:
                raise ValueError()
            policy = validate_question_policy(policy)
        except QuestionStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise QuestionStoreError("control_invalid") from exc
        if policy["policy_digest"] != policy_digest:
            raise QuestionStoreError("control_invalid")
        policies[policy_digest] = policy
    require_digest(value["active_deployment_digest"])
    if value["active_deployment_digest"] not in bindings:
        raise QuestionStoreError("control_invalid")
    if len(requests) > MAX_REQUESTS or type(value["reserved_microusd"]) is not int:
        raise QuestionStoreError("control_invalid")
    if value["reserved_microusd"] != len(requests) * REQUEST_MICROUSD:
        raise QuestionStoreError("control_invalid")
    for request_id, row in requests.items():
        require_request_id(request_id)
        if not isinstance(row, dict) or set(row) not in (
            _ADMISSION_FIELDS,
            _ADMISSION_FIELDS | {"execution"},
            _ADMISSION_FIELDS | {"execution", "result"},
        ):
            raise QuestionStoreError("control_invalid")
        for field in ("request_digest", "intake_digest", "policy_digest", "deployment_digest"):
            require_digest(row[field])
        if bindings.get(row["deployment_digest"]) != row["policy_digest"]:
            raise QuestionStoreError("control_invalid")
        if not isinstance(row["client_scope_id"], str) or not row["client_scope_id"].strip():
            raise QuestionStoreError("control_invalid")
        for field in ("request_generation", "intake_generation"):
            generation = row[field]
            if not isinstance(generation, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", generation):
                raise QuestionStoreError("control_invalid")
        if (
            type(row["reserved_microusd"]) is not int
            or row["reserved_microusd"] != REQUEST_MICROUSD
        ):
            raise QuestionStoreError("control_invalid")
        try:
            deadline = control_time(row["admitted_at"]) + timedelta(
                seconds=policies[row["policy_digest"]]["limits"]["deadline_seconds"]
            )
        except OverflowError as exc:
            raise QuestionStoreError("control_invalid") from exc
        if deadline != control_time(row["deadline_at"]):
            raise QuestionStoreError("control_invalid")
        if "execution" in row:
            validate_call_execution(
                row["execution"], row, request_id, policy=policies[row["policy_digest"]]
            )
        if "result" in row:
            pointer = row["result"]
            if type(pointer) is not dict or set(pointer) != {"digest", "generation"}:
                raise QuestionStoreError("control_invalid")
            require_digest(pointer["digest"])
            if type(pointer["generation"]) is not str or not re.fullmatch(
                r"[1-9][0-9]{0,19}", pointer["generation"]
            ):
                raise QuestionStoreError("control_invalid")
    return copy.deepcopy(value)


CALL_BINDING_FIELDS = {
    "request_id",
    "stage",
    "owner_nonce",
    "request_digest",
    "intake_digest",
    "policy_digest",
    "deployment_digest",
    "input_digest",
    "system_instruction_digest",
    "response_schema_digest",
    "model",
    "thinking_level",
    "counted_input_tokens",
    "max_output_tokens",
    "started_at",
}


def validate_call_execution(
    value: object, admission: dict, request_id: str, *, policy: dict
) -> None:
    if (
        type(value) is not dict
        or set(value) not in ({"calls"}, {"calls", "queries"})
        or type(value["calls"]) is not dict
    ):
        raise QuestionStoreError("control_invalid")
    if "queries" in value:
        queries = value["queries"]
        if type(queries) is not dict or len(queries) > 8:
            raise QuestionStoreError("control_invalid")
        for key, reservation in queries.items():
            validate_query_reservation(reservation, admission, request_id)
            if key != str(reservation["ordinal"]):
                raise QuestionStoreError("control_invalid")
        if (
            sum(row["maximum_bytes_billed"] for row in queries.values()) > 1000000000
            or sum(row["candidate_limit"] for row in queries.values()) > 1000
        ):
            raise QuestionStoreError("control_invalid")
    calls = value["calls"]
    if not set(calls) <= {"planning", "answering"}:
        raise QuestionStoreError("control_invalid")
    for stage, call in calls.items():
        if type(call) is not dict or set(call) != CALL_BINDING_FIELDS | {
            "response",
            "usage",
            "usage_status",
            "usage_owner_nonce",
            "metering_failed",
        }:
            raise QuestionStoreError("control_invalid")
        require_request_id(call["owner_nonce"])
        if call["request_id"] != request_id or call["stage"] != stage:
            raise QuestionStoreError("control_invalid")
        for field in ("request_digest", "intake_digest", "policy_digest", "deployment_digest"):
            if call[field] != admission[field]:
                raise QuestionStoreError("control_invalid")
        for field in ("input_digest", "system_instruction_digest", "response_schema_digest"):
            require_digest(call[field])
        if (
            call["model"] != policy["model"]
            or call["thinking_level"]
            not in {"planning": ("LOW", "MEDIUM"), "answering": ("LOW", "MEDIUM", "HIGH")}[stage]
        ):
            raise QuestionStoreError("control_invalid")
        for field, ceiling in (
            ("counted_input_tokens", 8000 if stage == "planning" else 32000),
            ("max_output_tokens", 800 if stage == "planning" else 4000),
        ):
            if type(call[field]) is not int or not 0 <= call[field] <= ceiling:
                raise QuestionStoreError("control_invalid")
        if call["max_output_tokens"] == 0 or not (
            control_time(admission["admitted_at"])
            <= control_time(call["started_at"])
            < control_time(admission["deadline_at"])
        ):
            raise QuestionStoreError("control_invalid")
        if (
            call["usage_status"] not in ("unknown", "pending", "acknowledged", "failed")
            or type(call["metering_failed"]) is not bool
        ):
            raise QuestionStoreError("control_invalid")
        if call["usage_owner_nonce"] is not None:
            require_request_id(call["usage_owner_nonce"])
        for field in ("response", "usage"):
            pointer = call[field]
            if pointer is not None:
                if type(pointer) is not dict or set(pointer) != {"generation", "digest"}:
                    raise QuestionStoreError("control_invalid")
                require_digest(pointer["digest"])
                if type(pointer["generation"]) is not str or not re.fullmatch(
                    r"[1-9][0-9]{0,19}", pointer["generation"]
                ):
                    raise QuestionStoreError("control_invalid")
        if call["usage_status"] == "failed" and not call["metering_failed"]:
            raise QuestionStoreError("control_invalid")
        if call["usage_status"] == "unknown":
            if call["usage"] is not None or call["usage_owner_nonce"] is not None:
                raise QuestionStoreError("control_invalid")
        elif call["response"] is None or call["usage"] is None or call["usage_owner_nonce"] is None:
            raise QuestionStoreError("control_invalid")
    if "answering" in calls and (
        "planning" not in calls
        or calls["planning"]["usage_status"] != "acknowledged"
        or calls["planning"]["metering_failed"]
    ):
        raise QuestionStoreError("control_invalid")


QUERY_TEMPLATES = frozenset(
    {
        "released_candidates_v1",
        "released_evidence_v1",
        "released_review_v1",
        "current_source_copy_v1",
        "protected_context_candidates_v1",
        "protected_context_enriched_v1",
        "protected_context_raw_v1",
        "protected_context_enriched_index_v1",
        "protected_context_raw_index_v1",
        "protected_context_enriched_v2",
        "protected_context_raw_v2",
        "protected_context_enriched_index_v2",
        "protected_context_raw_index_v2",
        "protected_context_enriched_v3",
        "protected_context_raw_v3",
        "protected_context_enriched_index_v3",
        "protected_context_raw_index_v3",
        "protected_context_enriched_v4",
        "protected_context_raw_v4",
        "protected_context_enriched_index_v4",
        "protected_context_raw_index_v4",
        "protected_context_result_v1",
        "protected_context_results_v1",
        "protected_context_partitions_v1",
        "protected_context_approval_v1",
        "protected_context_bridge_history_v1",
        "protected_context_bridge_collection_v1",
    }
)
QUERY_BINDING_FIELDS = frozenset(
    {
        "contract_version",
        "request_id",
        "run_id",
        "request_digest",
        "intake_digest",
        "policy_digest",
        "deployment_digest",
        "ordinal",
        "template_id",
        "sql_digest",
        "parameters_digest",
        "maximum_bytes_billed",
        "candidate_limit",
    }
)


def query_binding_digest(value: dict) -> str:
    fields = QUERY_BINDING_FIELDS
    if value.get("contract_version") == "general_question_query_reservation_v2":
        fields = fields | {"cap_version"}
    return canonical_digest({key: value[key] for key in fields})


def query_job_id(request_id: str, ordinal: int, digest: str) -> str:
    return f"gq_{UUID(request_id).hex}_{ordinal}_{digest}"


def validate_query_reservation(value: object, admission: dict, request_id: str) -> None:
    fields = QUERY_BINDING_FIELDS
    if (
        type(value) is dict
        and value.get("contract_version") == "general_question_query_reservation_v2"
    ):
        fields = fields | {"cap_version"}
        if type(value.get("cap_version")) is not int or value["cap_version"] < 1:
            raise QuestionStoreError("snapshot_cap_version_unknown")
    if type(value) is not dict or set(value) != fields | {
        "query_digest",
        "job_id",
        "reserved_at",
        "receipt",
    }:
        raise QuestionStoreError("control_invalid")
    if (
        value["contract_version"]
        not in {"general_question_query_reservation_v1", "general_question_query_reservation_v2"}
        or value["request_id"] != request_id
        or value["run_id"] != f"question_{UUID(request_id).hex}"
    ):
        raise QuestionStoreError("control_invalid")
    for key in ("request_digest", "intake_digest", "policy_digest", "deployment_digest"):
        if value[key] != admission[key]:
            raise QuestionStoreError("control_invalid")
    for key in ("sql_digest", "parameters_digest", "query_digest"):
        require_digest(value[key])
    for key, lower, upper in (
        ("ordinal", 1, 8),
        ("maximum_bytes_billed", 0, 1000000000),
        ("candidate_limit", 0, 1000),
    ):
        if type(value[key]) is not int or not lower <= value[key] <= upper:
            raise QuestionStoreError("control_invalid")
    if type(value["template_id"]) is not str or value["template_id"] not in QUERY_TEMPLATES:
        raise QuestionStoreError("control_invalid")
    digest = query_binding_digest(value)
    if value["query_digest"] != digest or value["job_id"] != query_job_id(
        request_id, value["ordinal"], digest
    ):
        raise QuestionStoreError("control_invalid")
    if (
        not control_time(admission["admitted_at"])
        <= control_time(value["reserved_at"])
        < control_time(admission["deadline_at"])
    ):
        raise QuestionStoreError("control_invalid")
    if value["receipt"] is not None:
        validate_query_receipt(value["receipt"], value)


def validate_query_receipt(value: object, reservation: dict) -> dict:
    if type(value) is not dict or set(value) != {
        "job_id",
        "template_id",
        "sql_digest",
        "parameters_digest",
        "billed_bytes",
        "observed_candidate_count",
        "result_digest",
        "query_state",
    }:
        raise QuestionStoreError("query_receipt_invalid")
    for key in ("job_id", "template_id", "sql_digest", "parameters_digest"):
        if value[key] != reservation[key]:
            raise QuestionStoreError("query_receipt_invalid")
    if type(value["query_state"]) is not str or value["query_state"] not in {
        "running",
        "succeeded",
        "failed",
    }:
        raise QuestionStoreError("query_receipt_invalid")
    for key, bound in (
        ("billed_bytes", "maximum_bytes_billed"),
        ("observed_candidate_count", "candidate_limit"),
    ):
        if value[key] is not None and (
            type(value[key]) is not int
            or value[key] < 0
            or (value["query_state"] != "failed" and value[key] > reservation[bound])
        ):
            raise QuestionStoreError("query_receipt_invalid")
    if value["query_state"] == "running" and any(
        value[key] is not None
        for key in ("billed_bytes", "observed_candidate_count", "result_digest")
    ):
        raise QuestionStoreError("query_receipt_invalid")
    if value["query_state"] == "succeeded":
        if value["billed_bytes"] is None or value["observed_candidate_count"] is None:
            raise QuestionStoreError("query_receipt_invalid")
        require_digest(value["result_digest"], "query_receipt_invalid")
    elif value["result_digest"] is not None:
        raise QuestionStoreError("query_receipt_invalid")
    return copy.deepcopy(value)
