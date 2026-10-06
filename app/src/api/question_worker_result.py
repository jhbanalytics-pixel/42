import hashlib
import json
import re
from datetime import datetime

from src.api.question_worker_protocol import (
    _digest,
    _read_reply,
    decode_execution_reply,
)


class ResultVerificationError(ValueError):
    pass


def decode_result_record(data, *, generation, invocation, receipt, max_bytes):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ResultVerificationError("result_limit_invalid")
    try:
        pointer = decode_execution_reply(
            json.dumps(receipt, allow_nan=False).encode("utf-8"), invocation
        )
        if (
            not isinstance(generation, str)
            or generation != pointer["result_generation"]
        ):
            raise ResultVerificationError("result_generation_mismatch")
        value = _read_reply(data, max_bytes=max_bytes)
        if hashlib.sha256(data).hexdigest() != pointer["result_digest"]:
            raise ResultVerificationError("result_digest_mismatch")
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if canonical != data:
            raise ResultVerificationError("result_encoding_invalid")
        if (
            set(value)
            != {
                "contract_version",
                "request_id",
                "request_digest",
                "intake_digest",
                "policy_digest",
                "deployment_digest",
                "plan_digest",
                "snapshot_digest",
                "state",
                "recorded_at",
                "response",
            }
            or value["contract_version"] != "general_question_result_record_v1"
        ):
            raise ResultVerificationError("result_envelope_invalid")
        if any(
            value[key] != invocation[key]
            for key in [
                "request_id",
                "request_digest",
                "intake_digest",
                "policy_digest",
                "deployment_digest",
            ]
        ):
            raise ResultVerificationError("result_identity_mismatch")
        state = value["state"]
        if not isinstance(state, str) or state not in {
            "complete",
            "partial",
            "needs_clarification",
            "unavailable",
            "refused",
            "held",
        }:
            raise ResultVerificationError("result_state_invalid")
        if (state == "held") != (pointer["state"] == "held"):
            raise ResultVerificationError("result_state_mismatch")
        for key in ["plan_digest", "snapshot_digest"]:
            digest = value[key]
            if not _digest(digest) and (
                digest is not None or state in {"complete", "partial"}
            ):
                raise ResultVerificationError("result_evidence_binding_invalid")
        recorded = value["recorded_at"]
        if (
            not isinstance(recorded, str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", recorded
            )
            is None
        ):
            raise ResultVerificationError("result_time_invalid")
        datetime.fromisoformat(recorded)
        response = value["response"]
        required = {"answer", "sources", "intelligence"}
        if (
            not isinstance(response, dict)
            or set(response) not in [required, required | {"error", "reason"}]
            or not isinstance(response["answer"], str)
            or not isinstance(response["sources"], list)
        ):
            raise ResultVerificationError("result_response_invalid")
        if "error" in response and (
            response["error"] is not True
            or not isinstance(response["reason"], str)
            or not response["reason"]
        ):
            raise ResultVerificationError("result_response_invalid")
        intelligence = response["intelligence"]
        if (
            not isinstance(intelligence, dict)
            or intelligence.get("contract_version") != "general_cultural_question_v1"
            or any(
                intelligence.get(key) != invocation[key]
                for key in ["request_id", "request_digest"]
            )
        ):
            raise ResultVerificationError("result_response_identity_mismatch")
        public_state = intelligence.get("status")
        if public_state in ["complete", "partial"] and not all(
            _digest(value[key]) for key in ["plan_digest", "snapshot_digest"]
        ):
            raise ResultVerificationError("result_evidence_binding_invalid")
        if state == "held":
            if (
                public_state not in ["unavailable", "partial"]
                or intelligence.get("ready_for_downstream") is not False
                or not isinstance(intelligence.get("usage"), dict)
                or intelligence["usage"].get("status") != "unresolved"
            ):
                raise ResultVerificationError("result_held_projection_invalid")
        elif public_state != state:
            raise ResultVerificationError("result_response_state_mismatch")
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, ResultVerificationError):
            raise
        raise ResultVerificationError("result_record_invalid") from None
    return value
