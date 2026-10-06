import json

import pytest

from src.api.question_worker_protocol import (
    WorkerProtocolError,
    decode_admission_reply,
    decode_execution_reply,
    decode_status_reply,
    encode_worker_input,
)

REQUEST_ID = "caa0f9c1-b95c-4821-8f21-454e46ece40d"


def invocation():
    return {
        "contract_version": "general_cultural_question_v1",
        "request_id": REQUEST_ID,
        "request_digest": "a" * 64,
        "intake_digest": "b" * 64,
        "policy_digest": "c" * 64,
        "deployment_digest": "d" * 64,
    }


def admission():
    return {
        "contract_version": "general_question_admission_v1",
        "request_id": REQUEST_ID,
        "transport": {
            "message": "Why are repairs becoming social? café",
            "history": [{"role": "user", "content": str(i)} for i in range(40)],
        },
        "scope": {
            "client_scope_id": "ogilvy_default",
            "market_scope": ["za", "ng", "ke"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        "selected_market": "za",
        "policy_digest": "c" * 64,
        "deployment_digest": "d" * 64,
    }


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def execution_reply():
    value = invocation()
    return {
        key: value[key] for key in ["request_id", "request_digest", "intake_digest"]
    } | {
        "result_digest": "e" * 64,
        "result_generation": "1780000000000000",
        "state": "held",
    }


def test_admission_preserves_untrimmed_transport_and_nullable_scope():
    original = admission()
    encoded = encode_worker_input("admit", original)
    assert b"caf\xc3\xa9" in encoded
    assert json.loads(encoded) == original
    assert len(json.loads(encoded)["transport"]["history"]) == 40
    assert "as_of" not in json.loads(encoded)


@pytest.mark.parametrize("anchor", [None, "ef06dcc1-79f5-490d-847f-868866d3ce83"])
def test_parent_admission_v2_transports_only_explicit_identities(anchor):
    value = admission() | {
        "contract_version": "general_question_admission_v2",
        "parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0",
        "thread_anchor_request_id": anchor,
    }
    assert json.loads(encode_worker_input("admit", value)) == value
    assert set(value["transport"]) == {"message", "history"}


@pytest.mark.parametrize("change", [
    {"parent_request_id": None}, {"parent_request_id": "AD30EFED-DD99-49DA-8113-3943E289FBA0"},
    {"parent_request_id": "ad30efeddd9949da81133943e289fba0"}, {"parent_request_id": 1},
    {"parent_request_id": {}}, {"thread_anchor_request_id": "bad"},
    {"parent_context_ref": {}}, {"parent_receipt_aliases": ["supplied"]},
])
def test_parent_admission_refuses_noncanonical_or_client_authority_fields(change):
    value = admission() | {
        "contract_version": "general_question_admission_v2",
        "parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0",
        "thread_anchor_request_id": None,
    }
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", value | change)


def test_parent_protocol_requires_version_and_both_outer_reference_fields():
    value = admission() | {
        "parent_request_id": "ad30efed-dd99-49da-8113-3943e289fba0",
        "thread_anchor_request_id": None,
    }
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", value)
    value["contract_version"] = "general_question_admission_v2"
    value.pop("thread_anchor_request_id")
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", value)


@pytest.mark.parametrize(
    "change",
    [
        {"as_of": "2026-09-07T00:00:00Z"},
        {"budget": 1},
        {"selected_market": "all"},
        {"request_id": "not-a-uuid"},
    ],
)
def test_admission_rejects_unagreed_or_invalid_fields(change):
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", admission() | change)


def test_execute_stdin_has_only_the_bound_six_fields():
    assert json.loads(encode_worker_input("execute", invocation())) == invocation()
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("execute", invocation() | {"message": "new request"})
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("execute", invocation() | {"policy_digest": "C" * 64})


def test_admission_reply_binds_uuid_policy_and_generations():
    value = {
        "contract_version": "general_question_admission_result_v1",
        "job_id": "chat_" + REQUEST_ID.replace("-", ""),
        "invocation": invocation(),
        "request_generation": "11",
        "intake_generation": "12",
        "deadline_at": "2026-09-07T10:03:00+00:00",
    }
    assert decode_admission_reply(wire(value), admission()) == value
    for change in [
        {"job_id": "chat_" + "0" * 32},
        {"request_generation": 11},
        {"intake_generation": "0"},
        {"deadline_at": "2026-09-07T10:03:00"},
        {"invocation": invocation() | {"deployment_digest": "f" * 64}},
    ]:
        with pytest.raises(WorkerProtocolError):
            decode_admission_reply(wire(value | change), admission())


def test_execution_reply_is_metadata_only_and_request_bound():
    value = execution_reply()
    assert decode_execution_reply(wire(value), invocation()) == value
    for change in [
        {"request_digest": "f" * 64},
        {"intake_digest": "f" * 64},
        {"result_generation": "0"},
        {"result_generation": True},
        {"state": "complete"},
        {"state": []},
        {"state": {}},
        {"answer": "unverified prose"},
    ]:
        with pytest.raises(WorkerProtocolError):
            decode_execution_reply(wire(value | change), invocation())


@pytest.mark.parametrize(
    "data",
    [
        b'{"state":"terminal","state":"held"}',
        b"{}\n{}",
        b'{"x":NaN}',
        b"\xff",
        b" " * 65537,
    ],
    ids=[
        "duplicate-key",
        "multiple-documents",
        "nonfinite",
        "invalid-utf8",
        "oversize",
    ],
)
def test_bad_stdout_never_becomes_an_execution_receipt(data):
    with pytest.raises(WorkerProtocolError):
        decode_execution_reply(data, invocation())


def test_input_cap_counts_utf8_bytes_without_trimming():
    value = admission()
    value["transport"]["message"] = "🎵" * 70000
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", value)


def status_request():
    return {
        "contract_version": "general_question_status_request_v1",
        "request_id": REQUEST_ID,
        "scope": admission()["scope"],
    }


def status_reply(state="admitted"):
    value = invocation()
    return {
        **value,
        "contract_version": "general_question_status_v1",
        "state": state,
        "deadline_at": "2026-09-07T10:03:00Z",
        "result_digest": None if state in {"admitted", "running"} else "e" * 64,
        "result_generation": None if state in {"admitted", "running"} else "12",
    }


def test_status_input_uses_only_current_trusted_scope_and_uuid():
    value = status_request()
    assert json.loads(encode_worker_input("status", value)) == value
    for change in [
        {"message": "new question"},
        {"as_of": "2026-09-08T00:00:00Z"},
        {"scope": {}},
        {"request_id": "bad"},
    ]:
        with pytest.raises(WorkerProtocolError):
            encode_worker_input("status", value | change)


@pytest.mark.parametrize(
    "state",
    [
        "admitted",
        "running",
        "complete",
        "partial",
        "needs_clarification",
        "unavailable",
        "refused",
        "held",
    ],
)
def test_status_reply_retains_durable_state_and_exact_result_pointer(state):
    value = status_reply(state)
    assert decode_status_reply(wire(value), status_request()) == value


@pytest.mark.parametrize(
    "change",
    [
        {"result_digest": "e" * 64},
        {"result_generation": "12"},
        {"request_digest": "bad"},
        {"deadline_at": "2026-09-07T10:03:00"},
        {"request_id": "00000000-0000-4000-8000-000000000001"},
        {"state": []},
        {"response": {}},
    ],
)
def test_invalid_status_does_not_become_pending_or_terminal(change):
    with pytest.raises(WorkerProtocolError):
        decode_status_reply(wire(status_reply() | change), status_request())


def test_terminal_status_requires_both_result_locators():
    for state in [
        "complete",
        "partial",
        "needs_clarification",
        "unavailable",
        "refused",
        "held",
    ]:
        with pytest.raises(WorkerProtocolError):
            decode_status_reply(
                wire(
                    status_reply(state)
                    | {"result_digest": None, "result_generation": None}
                ),
                status_request(),
            )


LENS_ENVELOPE = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": "e" * 64,
}


@pytest.mark.parametrize(
    "version", ["general_question_admission_v1", "general_question_admission_v2"]
)
def test_an_admission_carries_the_optional_client_lens_on_both_versions(version):
    value = admission() | {"contract_version": version, "client_lens": LENS_ENVELOPE}
    if version == "general_question_admission_v2":
        value |= {
            "parent_request_id": "b2a2c5da-33c1-4a0f-9b4f-3fd2d3f0a2a1",
            "thread_anchor_request_id": None,
        }
    assert json.loads(encode_worker_input("admit", value))["client_lens"] == LENS_ENVELOPE
    assert "client_lens" not in json.loads(encode_worker_input("admit", admission()))


@pytest.mark.parametrize(
    "client_lens",
    [
        {**LENS_ENVELOPE, "configuration_digest": "not-a-digest"},
        {**LENS_ENVELOPE, "configuration_digest": "e" * 63},
        {**LENS_ENVELOPE, "configuration_digest": None},
        {**LENS_ENVELOPE, "configuration_digest": "E" * 64},
        {**LENS_ENVELOPE, "client_lens_id": ""},
        {**LENS_ENVELOPE, "client_lens_id": None},
        {**LENS_ENVELOPE, "lens_binding_version": "client_lens_binding_v0"},
        {"client_lens_id": "bsa_pulse_lens", "configuration_digest": "e" * 64},
        {**LENS_ENVELOPE, "extra": "x"},
        "bsa_pulse_lens",
        [],
        {},
    ],
)
def test_a_malformed_client_lens_envelope_never_reaches_the_worker(client_lens):
    with pytest.raises(WorkerProtocolError):
        encode_worker_input("admit", admission() | {"client_lens": client_lens})
