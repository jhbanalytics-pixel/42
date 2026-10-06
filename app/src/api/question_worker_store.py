import json
import hashlib
import math
import time

from src.api.question_worker_protocol import (
    _read_reply,
    decode_execution_reply,
    decode_observe_reply,
    encode_worker_input,
)
from src.api.question_worker_result import ResultVerificationError, decode_result_record


_MAX_BYTES = 4 * 1024 * 1024
_BUCKET = "listening-post-staging-cache"
_PREFIX = "open-intelligence/v2/staging/general-questions/requests/"


def read_request_bytes(storage_client, *, invocation, deadline):
    encode_worker_input("execute", invocation)
    name = _PREFIX + invocation["request_id"] + "/request.json"
    data, _ = _read_object(storage_client, name, generation=None, deadline=deadline)
    return data


def read_result_record(storage_client, *, invocation, receipt, deadline):
    try:
        pointer = decode_execution_reply(
            json.dumps(receipt, allow_nan=False).encode("utf-8"), invocation
        )
        generation = int(pointer["result_generation"])
    except (ValueError, TypeError, RecursionError):
        raise ResultVerificationError("result_pointer_invalid") from None
    name = (
        _PREFIX
        + pointer["request_id"]
        + "/results/"
        + pointer["result_digest"]
        + ".json"
    )
    data, selected = _read_object(
        storage_client, name, generation=generation, deadline=deadline
    )
    result = decode_result_record(
        data,
        generation=selected,
        invocation=invocation,
        receipt=pointer,
        max_bytes=_MAX_BYTES,
    )
    if time.monotonic() >= deadline:
        raise ResultVerificationError("result_deadline_reached")
    return result


def read_observed_question_detail(
    storage_client, *, observe_input, observed_reply, deadline
):
    from src.api import fieldwork
    from src.api.question_worker_deadline import _FIELDS, LENS_FIELD, stored_request_lens

    try:
        observed = decode_observe_reply(
            json.dumps(observed_reply, allow_nan=False).encode("utf-8"), observe_input
        )
        if observed["mode"] != "detail":
            raise ValueError()
        invocation = observed["invocation"]
        values, raw_values = {}, {}
        for name in ("request", "intake"):
            raw, _ = _read_object(
                storage_client,
                _PREFIX + invocation["request_id"] + f"/{name}.json",
                generation=int(observed[name + "_generation"]),
                deadline=deadline,
            )
            value = _read_reply(raw, max_bytes=_MAX_BYTES)
            packed = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            digest_key = name + "_digest"
            unsigned = json.dumps(
                {key: item for key, item in value.items() if key != digest_key},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if (
                raw != packed
                or value.get("request_id") != invocation["request_id"]
                or value.get(digest_key) != invocation[digest_key]
                or hashlib.sha256(unsigned).hexdigest() != invocation[digest_key]
            ):
                raise ResultVerificationError("observation_binding_invalid")
            values[name], raw_values[name] = value, raw
        request, intake = values["request"], values["intake"]
        if (
            not _FIELDS <= set(request) <= _FIELDS | {LENS_FIELD}
            or request["contract_version"] != "general_cultural_question_v1"
            or request["policy_digest"] != invocation["policy_digest"]
            or request["run_id"]
            != "question_" + invocation["request_id"].replace("-", "")
            or intake["request_digest"] != invocation["request_digest"]
            or intake["captured_at"] != request["as_of"]
        ):
            raise ResultVerificationError("observation_binding_invalid")
        intake_fields = {
            "contract_version",
            "request_id",
            "request_digest",
            "intake_digest",
            "selected_market",
            "captured_at",
        }
        version = intake["contract_version"]
        if version == "general_question_intake_context_v2":
            intake_fields.add("parent_context_ref")
        elif version == "general_question_intake_context_v3":
            intake_fields.add("source_window_hint")
            if "parent_context_ref" in intake:
                intake_fields.add("parent_context_ref")
        elif version != "general_question_intake_context_v1":
            raise ResultVerificationError("observation_binding_invalid")
        if set(intake) != intake_fields or (
            intake["selected_market"] is not None
            and intake["selected_market"] not in request["market_scope"]
        ):
            raise ResultVerificationError("observation_binding_invalid")
        ceiling = observe_input["scope"]

        def scope_within(actual, allowed):
            fieldwork._scope_v2(actual)
            return (
                all(
                    actual[key] == allowed[key]
                    for key in ("client_scope_id", "brand_config_id", "theme_id")
                )
                and set(actual["audience_lens_ids"])
                == set(allowed["audience_lens_ids"])
                and set(actual["market_scope"]) <= set(allowed["market_scope"])
            )

        request_scope = {key: request[key] for key in fieldwork._SCOPE_FIELDS}
        if not scope_within(request_scope, ceiling):
            raise ResultVerificationError("observation_scope_invalid")
        record = None
        if observed["result_pointer"] is not None:
            record = read_result_record(
                storage_client,
                invocation=invocation,
                receipt={
                    **observed["result_pointer"],
                    "state": "held"
                    if observed["observed_state"] == "held"
                    else "terminal",
                },
                deadline=deadline,
            )
            resolved_scope = record["response"]["intelligence"]["resolved_scope"]
            selected_market = intake["selected_market"]
            if (
                record["state"] != observed["observed_state"]
                or not scope_within(resolved_scope, request_scope)
                or (
                    selected_market is not None
                    and resolved_scope["market_scope"] != [selected_market]
                    # The engine keeps the request's own markets for a result
                    # that expired before any plan was bound.
                    and not (
                        record["plan_digest"] is None
                        and resolved_scope["market_scope"]
                        == request_scope["market_scope"]
                    )
                )
            ):
                raise ResultVerificationError("observation_binding_invalid")
        plan_window = None
        if record is not None and record["plan_digest"] is not None:
            plan_window = _bound_plan_window(
                storage_client, invocation, record["plan_digest"], deadline
            )
        if time.monotonic() >= deadline:
            raise ResultVerificationError("result_deadline_reached")
        return {
            "request_bytes": raw_values["request"],
            "intake_bytes": raw_values["intake"],
            "result_record": record,
            # The window the stored plan names, or None when no plan is bound or
            # the plan could not resolve one. Expiry and cancel keep a stored
            # plan's digest even when that plan named no window, and the reply
            # then carries the engine's default window instead.
            "plan_window": plan_window,
            # The lens the request was admitted under, read from the request bytes
            # the invocation digest already binds, so the boundary that records it
            # never has to take a caller's word for it.
            "client_lens": stored_request_lens(request),
        }
    except ResultVerificationError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ResultVerificationError("observation_binding_invalid") from None


def _bound_plan_window(storage_client, invocation, plan_digest, deadline):
    """Read the plan a result record binds, and the window it names.

    The plan is written once, and the observer names no generation for it, so
    it is read as stored and held to the record by its digest instead.
    """
    raw, _ = _read_object(
        storage_client,
        _PREFIX + invocation["request_id"] + "/plan.json",
        generation=None,
        deadline=deadline,
    )
    plan = _read_reply(raw, max_bytes=_MAX_BYTES)

    def packed(value):
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    unsigned = {key: item for key, item in plan.items() if key != "plan_digest"}
    window = plan.get("window")
    if (
        raw != packed(plan)
        or plan.get("plan_digest") != plan_digest
        or hashlib.sha256(packed(unsigned)).hexdigest() != plan_digest
        or any(
            plan.get(key) != invocation[key]
            for key in ("request_id", "request_digest", "intake_digest")
        )
        or not (
            window is None
            or (
                type(window) is dict
                and set(window) == {"start", "end", "closed"}
                and all(type(window[key]) is str for key in ("start", "end"))
                and type(window["closed"]) is bool
            )
        )
    ):
        raise ResultVerificationError("observation_binding_invalid")
    return window


def _read_object(storage_client, name, *, generation, deadline):
    if type(deadline) not in {int, float} or not math.isfinite(deadline):
        raise ResultVerificationError("result_deadline_invalid")

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ResultVerificationError("result_deadline_reached")
        return seconds

    remaining()
    try:
        blob = storage_client.bucket(_BUCKET).blob(name, generation=generation)
        blob.reload(if_generation_match=generation, timeout=remaining(), retry=None)
        remaining()
        selected = blob.generation
        if (
            type(selected) is not int
            or selected <= 0
            or (generation is not None and selected != generation)
        ):
            raise ResultVerificationError("result_generation_mismatch")
        size = blob.size
        if type(size) is not int or not 0 <= size <= _MAX_BYTES:
            raise ResultVerificationError("result_size_invalid")
        data = blob.download_as_bytes(
            start=0,
            end=_MAX_BYTES,
            raw_download=True,
            if_generation_match=selected,
            timeout=remaining(),
            retry=None,
        )
        remaining()
        if blob.generation != selected:
            raise ResultVerificationError("result_generation_mismatch")
        if not isinstance(data, bytes) or len(data) != size:
            raise ResultVerificationError("result_size_mismatch")
    except ResultVerificationError:
        raise
    except Exception:
        raise ResultVerificationError("result_store_unavailable") from None
    remaining()
    return data, str(selected)
