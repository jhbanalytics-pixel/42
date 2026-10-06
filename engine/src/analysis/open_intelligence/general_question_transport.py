"""Controlled async Vertex transport; execution authority remains with the worker."""

from __future__ import annotations

import copy
import math
from contextlib import asynccontextmanager

import httpx
from google import genai
from google.auth.credentials import Credentials
from google.genai import types


def question_call_options(*, remaining_seconds) -> types.HttpOptions:
    if (
        type(remaining_seconds) not in (int, float)
        or remaining_seconds <= 0
        or (isinstance(remaining_seconds, float) and not math.isfinite(remaining_seconds))
    ):
        raise ValueError("model_timeout")
    milliseconds = max(1, math.floor(min(remaining_seconds, 60) * 1000))
    return types.HttpOptions(
        timeout=milliseconds,
        retry_options=types.HttpRetryOptions(attempts=1),
        headers={"X-Server-Timeout": str(math.ceil(milliseconds / 1000))},
    )


@asynccontextmanager
async def create_question_model_client(*, credentials):
    if not isinstance(credentials, Credentials):
        raise ValueError("question credentials are required")
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        client = genai.Client(
            vertexai=True,
            project="ogilvy-trends-v2",
            location="global",
            credentials=credentials,
            http_options=types.HttpOptions(
                base_url="https://aiplatform.googleapis.com/",
                api_version="v1",
                timeout=60000,
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_async_client=http_client,
                client_args={"follow_redirects": False, "trust_env": False},
            ),
        )
        try:
            yield client
        finally:
            try:
                await client.aio.aclose()
            finally:
                client.close()


def _ordered_response_schema(response_schema, *, omit_length_constraints=False):
    schema = copy.deepcopy(response_schema)
    if omit_length_constraints:
        for field in ("minItems", "maxItems", "minLength", "maxLength"):
            schema.pop(field, None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema.setdefault("property_ordering", list(properties))
        for key, child in properties.items():
            if isinstance(child, dict):
                properties[key] = _ordered_response_schema(
                    child, omit_length_constraints=omit_length_constraints
                )
    items = schema.get("items")
    if isinstance(items, dict):
        schema["items"] = _ordered_response_schema(
            items, omit_length_constraints=omit_length_constraints
        )
    for key in ("anyOf", "any_of"):
        branches = schema.get(key)
        if isinstance(branches, list):
            schema[key] = [
                _ordered_response_schema(branch, omit_length_constraints=omit_length_constraints)
                if isinstance(branch, dict)
                else branch
                for branch in branches
            ]
    return schema


def question_model_configs(
    *, system_instruction, response_schema, thinking_level, max_output_tokens, remaining_seconds
) -> tuple[types.CountTokensConfig, types.GenerateContentConfig]:
    if (
        not isinstance(system_instruction, str)
        or not system_instruction.strip()
        or not isinstance(response_schema, dict)
        or not response_schema
        or thinking_level not in ("LOW", "MEDIUM", "HIGH")
        or type(max_output_tokens) is not int
        or max_output_tokens <= 0
    ):
        raise ValueError("question model config is invalid")
    ordered_schema = _ordered_response_schema(response_schema)
    count_tokens_config = types.CountTokensConfig(
        system_instruction=system_instruction,
        generation_config=types.GenerationConfig(
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=copy.deepcopy(ordered_schema),
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        ),
        http_options=question_call_options(remaining_seconds=remaining_seconds),
    )
    generation_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=copy.deepcopy(ordered_schema),
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        http_options=question_call_options(remaining_seconds=remaining_seconds),
    )
    return count_tokens_config, generation_config
