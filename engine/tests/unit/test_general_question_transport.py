import asyncio
import copy
import importlib
import json
import socket

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials
from google.genai import errors, types


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_transport")


def credentials():
    value = AnonymousCredentials()
    value.token = "synthetic-anonymous"
    return value


def configs(**overrides):
    return module().question_model_configs(
        **{
            "system_instruction": "Synthetic system instruction.",
            "response_schema": {
                "type": "OBJECT",
                "properties": {"ok": {"type": "BOOLEAN"}},
                "required": ["ok"],
            },
            "thinking_level": "MEDIUM",
            "max_output_tokens": 8,
            "remaining_seconds": 60.0,
            **overrides,
        }
    )


@pytest.mark.parametrize("remaining", [0, -1, True, None, float("nan"), float("inf"), "1"])
def test_call_options_reject_invalid_remaining_time(remaining):
    with pytest.raises(ValueError):
        module().question_call_options(remaining_seconds=remaining)


@pytest.mark.parametrize(
    "remaining,expected_ms,server_seconds",
    [(60, 60000, "60"), (180, 60000, "60"), (0.25, 250, "1"), (0.0001, 1, "1")],
)
def test_call_options_are_positive_bounded_and_one_attempt(remaining, expected_ms, server_seconds):
    options = module().question_call_options(remaining_seconds=remaining)
    assert options.timeout == expected_ms
    assert options.retry_options.attempts == 1
    assert options.headers == {"X-Server-Timeout": server_seconds}


@pytest.mark.parametrize(
    "override",
    [
        {"thinking_level": "MINIMAL"},
        {"thinking_level": "UNKNOWN"},
        {"max_output_tokens": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": 1.5},
    ],
)
def test_model_configs_reject_invalid_thinking_and_output_size(override):
    with pytest.raises(ValueError):
        configs(**override)


def test_configs_keep_system_schema_and_disable_function_execution():
    count, generation = configs(thinking_level="HIGH")
    assert isinstance(count, types.CountTokensConfig)
    assert isinstance(generation, types.GenerateContentConfig)
    assert count.system_instruction == generation.system_instruction
    assert count.generation_config.response_schema == types.Schema.model_validate(
        generation.response_schema
    )
    assert count.generation_config.thinking_config == generation.thinking_config
    assert count.generation_config.max_output_tokens == generation.max_output_tokens
    assert generation.automatic_function_calling.disable is True
    assert generation.tools is None
    assert count.http_options.retry_options.attempts == 1
    assert generation.http_options.retry_options.attempts == 1


def intercept(monkeypatch, handler):
    constructed = []

    def factory(**kwargs):
        constructed.append(kwargs)
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", factory)
    monkeypatch.setattr(
        socket.socket, "connect", lambda *_args: pytest.fail("socket connection attempted")
    )
    return constructed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 307])
async def test_actual_sdk_sends_one_generation_request_for_error_or_redirect(monkeypatch, status):
    observed = []

    def handler(request):
        observed.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://must-not-follow.invalid/"},
            json={
                "error": {"code": status, "message": "synthetic failure", "status": "UNAVAILABLE"}
            },
        )

    constructed = intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        http_client = client._api_client._http_options.httpx_async_client
        assert client.vertexai is True
        assert client._api_client.project == "ogilvy-trends-v2"  # gitleaks:allow, public fixture project identifier
        assert client._api_client.location == "global"
        assert client._api_client._http_options.api_version == "v1"
        assert client._api_client._http_options.retry_options.attempts == 1
        assert http_client.follow_redirects is False
        _, generation = configs()
        with pytest.raises(errors.APIError) as raised:
            await client.aio.models.generate_content(
                model="gemini-3.5-flash", contents="Synthetic", config=generation
            )
        assert raised.value.code == status
    assert len(observed) == 1
    assert observed[0].method == "POST"
    assert observed[0].url.path.endswith(":generateContent")
    assert constructed == [{"retries": 0, "trust_env": False}]
    assert http_client.is_closed is True


@pytest.mark.asyncio
async def test_actual_sdk_retains_full_count_configuration_and_fresh_call_timeout(monkeypatch):
    observed = []

    def handler(request):
        observed.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
                "timeout": request.extensions["timeout"],
                "server_timeout": request.headers["X-Server-Timeout"],
            }
        )
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 3})
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]}}]
            },
        )

    intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        count, generation = configs(
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "window": {
                        "type": "OBJECT",
                        "properties": {
                            "start": {"type": "STRING"},
                            "end": {"type": "STRING"},
                        },
                        "required": ["start", "end"],
                    },
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "id": {"type": "STRING"},
                                "value": {"type": "NUMBER"},
                            },
                            "required": ["id", "value"],
                        },
                    },
                },
                "required": ["window", "items"],
            }
        )
        response = await client.aio.models.count_tokens(
            model="gemini-3.5-flash", contents="Synthetic", config=count
        )
        assert response.total_tokens == 3
        generation.http_options = module().question_call_options(remaining_seconds=0.25)
        response = await client.aio.models.generate_content(
            model="gemini-3.5-flash", contents="Synthetic", config=generation
        )
        assert response.parsed == {"ok": True}
    assert len(observed) == 2
    assert observed[0]["body"]["systemInstruction"] == observed[1]["body"]["systemInstruction"]
    assert observed[0]["body"]["generationConfig"] == observed[1]["body"]["generationConfig"]
    assert "responseSchema" in observed[0]["body"]["generationConfig"]
    assert "thinkingConfig" in observed[0]["body"]["generationConfig"]
    assert observed[0]["server_timeout"] == "60"
    assert observed[1]["server_timeout"] == "1"
    assert observed[1]["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("union_key", [None, "anyOf", "any_of"])
async def test_actual_sdk_preserves_explicit_schema_property_ordering(monkeypatch, union_key):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 3})
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": '{"b":true}'}]}}]
            },
        )

    field = (
        {"type": "STRING"}
        if union_key is None
        else {
            union_key: [
                {
                    "type": "OBJECT",
                    "properties": {"second": {"type": "STRING"}, "first": {"type": "BOOLEAN"}},
                    "required": ["second", "first"],
                }
            ]
        }
    )
    intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        count, generation = configs(
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "a": field,
                    "b": {"type": "BOOLEAN"},
                },
                "required": ["a", "b"],
                "property_ordering": ["b", "a"],
            }
        )
        await client.aio.models.count_tokens(
            model="gemini-3.5-flash", contents="Synthetic", config=count
        )
        await client.aio.models.generate_content(
            model="gemini-3.5-flash", contents="Synthetic", config=generation
        )
    count_schema = observed[0]["generationConfig"]["responseSchema"]
    generation_schema = observed[1]["generationConfig"]["responseSchema"]
    assert count_schema == generation_schema
    assert count_schema["property_ordering"] == ["b", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [True, False])
async def test_function_call_response_cannot_trigger_hidden_generation(monkeypatch, guarded):
    requests = []
    tool_calls = []

    def local_probe() -> str:
        """Return a synthetic result."""
        tool_calls.append("called")
        return "synthetic"

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [{"functionCall": {"name": "local_probe", "args": {}}}],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]}}]
            },
        )

    intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        _, generation = configs()
        generation.tools = [local_probe]
        if not guarded:
            generation.automatic_function_calling = types.AutomaticFunctionCallingConfig(
                disable=False, maximum_remote_calls=2
            )
        await client.aio.models.generate_content(
            model="gemini-3.5-flash", contents="Synthetic", config=generation
        )
    assert len(requests) == (1 if guarded else 2)
    assert tool_calls == ([] if guarded else ["called"])


@pytest.mark.asyncio
async def test_outer_deadline_cancels_actual_async_transport_without_retry(monkeypatch):
    requests = []
    cancelled = []

    async def handler(request):
        requests.append(request)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={})

    intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        _, generation = configs()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await client.aio.models.generate_content(
                    model="gemini-3.5-flash", contents="Synthetic", config=generation
                )
    assert len(requests) == 1
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_factory_requires_explicit_credentials_without_lookup(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network attempted"))
    with pytest.raises(ValueError):
        async with module().create_question_model_client(credentials=None):
            pytest.fail("implicit credentials admitted")


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["environment", "global_default"])
async def test_factory_pins_official_vertex_endpoint_despite_sdk_overrides(monkeypatch, override):
    from google.genai import _base_url

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": '{"ok":true}'}]}}]
            },
        )

    intercept(monkeypatch, handler)
    monkeypatch.setattr(_base_url, "_default_base_vertex_url", None)
    monkeypatch.setenv("GOOGLE_VERTEX_BASE_URL", "https://environment-override.invalid/injected")
    if override == "global_default":
        monkeypatch.setattr(
            _base_url, "_default_base_vertex_url", "https://global-override.invalid/injected"
        )
    async with module().create_question_model_client(credentials=credentials()) as client:
        _, generation = configs()
        await client.aio.models.generate_content(
            model="gemini-3.5-flash", contents="Synthetic", config=generation
        )
    assert len(requests) == 1
    assert (
        str(requests[0].url)
        == "https://aiplatform.googleapis.com/v1/projects/ogilvy-trends-v2/locations/global/publishers/google/models/gemini-3.5-flash:generateContent"
    )


def test_low_profile_reaches_both_sdk_configs():
    count, generation = configs(thinking_level="LOW")
    assert count.generation_config.thinking_config.thinking_level.value == "LOW"
    assert generation.thinking_config.thinking_level.value == "LOW"


@pytest.mark.asyncio
async def test_38_actual_sdk_wire_keeps_structured_low_without_unsupported_parameters(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 8})
        return httpx.Response(200, json={"modelVersion": "gemini-3.8-flash", "candidates": []})

    intercept(monkeypatch, handler)
    async with module().create_question_model_client(credentials=credentials()) as client:
        count, generation = configs(thinking_level="LOW")
        await client.aio.models.count_tokens(
            model="gemini-3.8-flash", contents="Synthetic", config=count
        )
        await client.aio.models.generate_content(
            model="gemini-3.8-flash", contents="Synthetic", config=generation
        )
    assert len(requests) == 2
    for request in requests:
        assert str(request.url).startswith(
            "https://aiplatform.googleapis.com/v1/projects/ogilvy-trends-v2/locations/global/"
            "publishers/google/models/gemini-3.8-flash:"
        )
        body = json.loads(request.content)
        config = body["generationConfig"]
        assert config["thinkingConfig"]["thinking_level"] == "LOW"
        assert config["responseMimeType"] == "application/json"
        assert config["responseSchema"]["required"] == ["ok"]
        assert not set(config) & {
            "temperature",
            "topP",
            "topK",
            "candidateCount",
            "frequencyPenalty",
            "presencePenalty",
        }
        assert "x-goog-api-key" not in request.headers


def test_default_ordered_schema_is_unchanged_by_answer_only_projection():
    from src.analysis.open_intelligence.general_question_planning import (
        GENERAL_QUESTION_PLAN_SCHEMA,
    )
    from src.analysis.open_intelligence.general_question_transport import _ordered_response_schema

    original = copy.deepcopy(GENERAL_QUESTION_PLAN_SCHEMA)
    expected = _ordered_response_schema(original)
    _ordered_response_schema(original, omit_length_constraints=True)
    assert _ordered_response_schema(original) == expected
    assert original == GENERAL_QUESTION_PLAN_SCHEMA
