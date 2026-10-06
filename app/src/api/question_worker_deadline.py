import hashlib
import json
import math
import re
from datetime import datetime, timedelta

from src.api.question_worker_process import WorkerProcessError
from src.api.question_worker_protocol import (
    WorkerProtocolError,
    _read_reply,
    encode_worker_input,
)
from src.api.question_worker_store import _MAX_BYTES


_FIELDS = {
    "contract_version",
    "request_id",
    "request_digest",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "question",
    "decision",
    "history",
    "history_omitted_turns",
    "requested_window",
    "as_of",
    "output_form",
    "policy_digest",
}
# The optional client lens the request was admitted under. It is written only
# when the request named one, so a general 42 record keeps the exact shape and
# bytes it had before lenses existed.
LENS_FIELD = "client_lens"
_LENS_FIELDS = {"lens_binding_version", "client_lens_id", "configuration_digest"}
_LENS_BINDING_VERSION = "client_lens_binding_v1"
_LENS_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
QUESTION_DEADLINE_SECONDS = 240


def stored_request_lens(request):
    """The lens a stored request carries, or None; a malformed one is a refusal."""
    if not isinstance(request, dict) or LENS_FIELD not in request:
        return None
    lens = request[LENS_FIELD]
    if (
        not isinstance(lens, dict)
        or set(lens) != _LENS_FIELDS
        or lens["lens_binding_version"] != _LENS_BINDING_VERSION
        or type(lens["client_lens_id"]) is not str
        or not lens["client_lens_id"]
        or type(lens["configuration_digest"]) is not str
        or _LENS_DIGEST.fullmatch(lens["configuration_digest"]) is None
    ):
        raise WorkerProtocolError("worker_request_invalid")
    return dict(lens)


def deadline_from_request(data, invocation, *, now, monotonic_now):
    encode_worker_input("execute", invocation)
    if (
        not isinstance(now, datetime)
        or now.utcoffset() is None
        or type(monotonic_now) not in {int, float}
        or not math.isfinite(monotonic_now)
    ):
        raise WorkerProcessError("worker_clock_invalid")
    try:
        value = _read_reply(data, max_bytes=_MAX_BYTES)
        if (
            not _FIELDS <= set(value) <= _FIELDS | {LENS_FIELD}
            or value["contract_version"] != "general_cultural_question_v1"
        ):
            raise WorkerProtocolError("worker_request_invalid")
        stored_request_lens(value)
        if any(
            value[key] != invocation[key]
            for key in ["request_id", "request_digest", "policy_digest"]
        ):
            raise WorkerProtocolError("worker_request_identity_mismatch")
        if value["run_id"] != "question_" + invocation["request_id"].replace("-", ""):
            raise WorkerProtocolError("worker_request_identity_mismatch")
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if canonical != data:
            raise WorkerProtocolError("worker_request_encoding_invalid")
        unsigned = {key: item for key, item in value.items() if key != "request_digest"}
        encoded = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != value["request_digest"]:
            raise WorkerProtocolError("worker_request_digest_mismatch")
        stamp = value["as_of"]
        if (
            not isinstance(stamp, str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", stamp
            )
            is None
        ):
            raise WorkerProtocolError("worker_request_time_invalid")
        as_of = datetime.fromisoformat(stamp)
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, WorkerProtocolError):
            raise
        raise WorkerProtocolError("worker_request_invalid") from None
    if as_of > now:
        raise WorkerProtocolError("worker_request_future")
    seconds = (
        as_of + timedelta(seconds=QUESTION_DEADLINE_SECONDS) - now
    ).total_seconds()
    if seconds <= 0:
        raise WorkerProcessError("worker_deadline_reached")
    return monotonic_now + seconds
