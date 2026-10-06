"""Bounded readback of one long running operation.

The reader is the only native touchpoint. It receives the operation name and a
per read timeout and returns the operation identity, a state and whatever
native evidence it has. The loop never resubmits anything and never turns a
deadline into success.

A read that could not be completed at the transport level is an unknown state
worth another read. A read the endpoint answered, or that the reader refused by
name, is a refusal: repeating it spends the whole deadline replaying an
authenticated request at an endpoint that has already said no, and still ends
unproven. The rule is stated by exception class rather than by counting
repeats, so a permission denial, a refused redirect and a response that arrived
from another url each end the loop after one read.
"""

import math
import urllib.error

# A socket failure, a name resolution failure, a connection reset or a local
# permission failure means the read was never answered, so another read is
# worth making. An answered HTTP status is not one of those, even though
# urllib models it as an OSError subclass.
_UNANSWERED = (OSError,)
_ANSWERED = (urllib.error.HTTPError,)

STATES = frozenset({"pending", "succeeded", "failed", "unknown"})
MAX_READ_SECONDS = 30.0


def _positive(value, code):
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(code)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(code)
    return float(value)


def _read(reader, operation_name, timeout_seconds):
    try:
        response = reader(operation_name, timeout_seconds)
    except Exception as error:
        if not isinstance(error, _UNANSWERED) or isinstance(error, _ANSWERED):
            # A refusal by name, a native error and an answered status are each
            # a refusal: none is an unknown state the loop may spend its whole
            # deadline repeating with a bearer token attached.
            raise
        return {
            "state": "unknown",
            "error": f"{type(error).__name__}: {error}",
        }, "unknown"
    if not isinstance(response, dict):
        raise ValueError("operation_response_invalid")
    if response.get("operation") != operation_name:
        raise ValueError("operation_identity_mismatch")
    state = response.get("state")
    if state not in STATES:
        raise ValueError("operation_state_invalid")
    return response, state


def wait_operation(
    reader,
    operation_name: str,
    *,
    deadline_seconds: float,
    poll_seconds: float,
    clock,
    sleep,
) -> dict:
    if not isinstance(operation_name, str) or not operation_name.strip():
        raise ValueError("operation_invalid")
    deadline_seconds = _positive(deadline_seconds, "deadline_invalid")
    poll_seconds = _positive(poll_seconds, "poll_invalid")
    if not callable(reader) or not callable(clock) or not callable(sleep):
        raise ValueError("readback_callables_invalid")
    started = float(clock())
    deadline = started + deadline_seconds
    reads = 0
    trail = []
    last_state = None
    last_error = None
    last_response = None
    while True:
        now = float(clock())
        remaining = deadline - now
        if remaining <= 0:
            break
        timeout_seconds = min(remaining, MAX_READ_SECONDS)
        response, state = _read(reader, operation_name, timeout_seconds)
        reads += 1
        finished = float(clock())
        last_state = state
        last_error = response.get("error")
        last_response = response
        trail.append(
            {
                "read": reads,
                "state": state,
                "timeout_seconds": timeout_seconds,
                "started_at": now - started,
                "finished_at": finished - started,
                "error": last_error,
            }
        )
        if state in {"succeeded", "failed"}:
            return {
                "operation": operation_name,
                "state": state,
                "reads": reads,
                "elapsed_seconds": finished - started,
                "deadline_seconds": deadline_seconds,
                "last_state": state,
                "last_error": last_error,
                "last_response": response,
                "trail": trail,
                "resubmitted": False,
            }
        remaining = deadline - finished
        if remaining <= 0:
            break
        sleep(min(poll_seconds, remaining))
    return {
        "operation": operation_name,
        "state": "unproven",
        "reads": reads,
        "elapsed_seconds": float(clock()) - started,
        "deadline_seconds": deadline_seconds,
        "last_state": last_state,
        "last_error": last_error,
        "last_response": last_response,
        "trail": trail,
        "resubmitted": False,
    }
