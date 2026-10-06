import copy
import importlib
import json
import socket
from datetime import timedelta

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials

from tests.unit import test_general_question_calls as calls_fixture


def codec():
    return importlib.import_module("src.analysis.open_intelligence.general_question_response")


def body(text='{"ok":true}'):
    return {
        "modelVersion": "gemini-3.5-flash",
        "usageMetadata": {
            "promptTokenCount": 500,
            "candidatesTokenCount": 100,
            "thoughtsTokenCount": 20,
            "totalTokenCount": 620,
        },
        "candidates": [
            {"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": text}]}}
        ],
    }


def raw(value=None):
    value = body() if value is None else value
    return {
        "sdk_http_response": {"body": json.dumps(value) if isinstance(value, dict) else value},
        "model_version": None,
        "usage_metadata": None,
        "candidates": None,
    }


def test_native_and_raw_modes_preserve_usage_and_input():
    native = calls_fixture.native()
    native["candidates"][0]["finish_reason"] = "STOP"
    original = copy.deepcopy(native)
    decoded = codec().decode_question_response(native)
    assert decoded["usage_metadata"]["thoughts_token_count"] == 20
    assert native == original
    response = raw()
    original = copy.deepcopy(response)
    decoded = codec().decode_question_response(response)
    assert decoded["model_version"] == "gemini-3.5-flash"
    assert decoded["usage_metadata"] == {
        "prompt_token_count": 500,
        "candidates_token_count": 100,
        "thoughts_token_count": 20,
        "total_token_count": 620,
    }
    assert codec().question_response_text(response) == '{"ok":true}'
    assert response == original
    decoded["usage_metadata"]["prompt_token_count"] = 1
    assert response == original


@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        '{"n":' + "9" * 5000 + "}",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        '["not an object"]',
        '{"x":"\\ud800"}',
    ],
    ids=[
        "malformed",
        "oversized_integer",
        "duplicate_key",
        "nan",
        "infinite_float",
        "array",
        "invalid_unicode",
    ],
)
def test_bad_semantic_json_never_blocks_known_usage(text):
    response = raw(body(text))
    assert codec().decode_question_response(response)["usage_metadata"]["total_token_count"] == 620
    with pytest.raises(ValueError):
        codec().question_response_text(response)


@pytest.mark.parametrize(
    "value",
    [
        '{"modelVersion":"a","modelVersion":"b"}',
        '{"usageMetadata":{"promptTokenCount":NaN}}',
        "{bad json",
        b'{"modelVersion":"a"}',
        "[]",
        '{"x":"\\ud800"}',
    ],
)
def test_malformed_outer_envelope_refuses_without_mutation(value):
    response = raw(value)
    before = copy.deepcopy(response)
    with pytest.raises(ValueError):
        codec().decode_question_response(response)
    assert response == before


@pytest.mark.parametrize(
    "mutation",
    [
        "multiple",
        "no_text",
        "function",
        "media",
        "code",
        "thought",
        "thought_number",
        "signature_type",
        "role",
    ],
)
def test_unsafe_plan_parts_do_not_prevent_usage_extraction(mutation):
    value = body()
    part = value["candidates"][0]["content"]["parts"][0]
    if mutation == "multiple":
        value["candidates"] *= 2
    elif mutation == "no_text":
        del part["text"]
    elif mutation == "role":
        value["candidates"][0]["content"]["role"] = "user"
    else:
        key, content = {
            "function": ("functionCall", {"name": "hidden"}),
            "media": ("inlineData", {"data": "AAAA"}),
            "code": ("executableCode", {"code": "hidden"}),
            "thought": ("thought", True),
            "thought_number": ("thought", 0),
            "signature_type": ("thoughtSignature", 1),
        }[mutation]
        part[key] = content
    response = raw(value)
    assert codec().decode_question_response(response)["usage_metadata"]["total_token_count"] == 620
    with pytest.raises(ValueError):
        codec().question_response_text(response)


def test_exact_concatenation_allows_inert_signature_metadata():
    value = body()
    value["candidates"][0]["content"]["parts"] = [
        {"text": ' \n{"ok":', "thought": False, "thoughtSignature": "c2ln"},
        {"text": "true}\n", "thought": None},
    ]
    assert codec().question_response_text(raw(value)) == ' \n{"ok":true}\n'
    native = calls_fixture.native()
    native["candidates"][0]["finish_reason"] = "STOP"
    native["candidates"][0]["content"]["parts"][0].update(
        text='{"ok":true}', thought_signature="c2ln"
    )
    assert codec().question_response_text(native) == '{"ok":true}'


@pytest.mark.parametrize(
    "field", ["promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount"]
)
def test_missing_usage_is_not_zero(field):
    value = body()
    del value["usageMetadata"][field]
    decoded = codec().decode_question_response(raw(value))
    assert len(decoded["usage_metadata"]) == 3
    assert {
        "promptTokenCount": "prompt_token_count",
        "candidatesTokenCount": "candidates_token_count",
        "thoughtsTokenCount": "thoughts_token_count",
        "totalTokenCount": "total_token_count",
    }[field] not in decoded["usage_metadata"]


@pytest.mark.parametrize("finish", ["SAFETY", "MAX_TOKENS", None, "conflicting_alias"])
def test_nonstop_response_retains_usage_but_cannot_become_plan(finish):
    value = body()
    if finish is None:
        del value["candidates"][0]["finishReason"]
    elif finish == "conflicting_alias":
        value["candidates"][0]["finish_reason"] = "SAFETY"
    else:
        value["candidates"][0]["finishReason"] = finish
    _module, calls, _, rid = calls_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    response = raw(value)
    snapshot = calls.record_response(
        permit,
        scope=calls_fixture.f.scope(),
        response=response,
        received_at=calls_fixture.f.NOW + timedelta(seconds=1),
    )
    event = calls.persist_usage(
        rid, stage="planning", scope=calls_fixture.f.scope(), persist=lambda event: event
    )
    assert event.completion_tokens == 120
    with pytest.raises(ValueError):
        codec().question_response_text(response)
    assert calls.read_response(rid, stage="planning", scope=calls_fixture.f.scope()) == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ['{"ok":true}', "not JSON", '{"n":' + "9" * 5000 + "}", None],
    ids=["valid_plan", "malformed_plan", "oversized_integer", "malformed_envelope"],
)
async def test_actual_sdk_raw_capture_is_stored_before_usage_and_plan_decode(monkeypatch, text):
    from src.analysis.open_intelligence.general_question_transport import (
        create_question_model_client,
        question_model_configs,
    )

    _module, calls, _, rid = calls_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    attempts = []

    def handler(request):
        attempts.append(request)
        if text is None:
            return httpx.Response(
                200, text="{bad json", headers={"Content-Type": "application/json"}
            )
        return httpx.Response(200, json=body(text))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(handler))
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    credentials = AnonymousCredentials()
    credentials.token = "synthetic-anonymous"
    async with create_question_model_client(credentials=credentials) as client:
        _, config = question_model_configs(
            system_instruction="Synthetic",
            response_schema={"type": "OBJECT"},
            thinking_level="MEDIUM",
            max_output_tokens=permit.max_output_tokens,
            remaining_seconds=60,
        )
        config.should_return_http_response = True
        response = await client.aio.models.generate_content(
            model=permit.model, contents="Synthetic", config=config
        )
    original = response.model_dump(mode="json")
    snapshot = calls.record_response(
        permit,
        scope=calls_fixture.f.scope(),
        response=original,
        received_at=calls_fixture.f.NOW + timedelta(seconds=1),
    )
    assert snapshot["raw_sdk_response"] == original
    events = []
    if text is None:
        with pytest.raises(_module.QuestionStoreError, match="usage_unknown"):
            calls.persist_usage(
                rid,
                stage="planning",
                scope=calls_fixture.f.scope(),
                persist=lambda event: events.append(event) or event,
            )
        assert events == []
    else:
        event = calls.persist_usage(
            rid,
            stage="planning",
            scope=calls_fixture.f.scope(),
            persist=lambda event: events.append(event) or event,
        )
        assert event.completion_tokens == 120
        assert len(events) == 1
    if text == '{"ok":true}':
        assert codec().question_response_text(original) == text
    else:
        with pytest.raises(ValueError):
            codec().question_response_text(original)
    assert (
        calls.read_response(rid, stage="planning", scope=calls_fixture.f.scope())[
            "raw_sdk_response"
        ]
        == original
    )
    assert len(attempts) == 1
