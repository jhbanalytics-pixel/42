"""Construct Gemini canary generation and full token-preflight requests locally."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.genai import types

from src.analysis.open_intelligence.canary_policy import CanaryPolicy

CANARY_PROJECT = "ogilvy-trends-v2"
CANARY_LOCATION = "global"
CANARY_API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class CanaryRequest:
    model: str
    contents: str
    count_tokens_config: types.CountTokensConfig
    generation_config: types.GenerateContentConfig
    input_digest: str
    system_instruction_digest: str
    response_schema_digest: str


def _prompt_contract(stage: str) -> tuple[str, dict[str, Any], str, str]:
    if stage == "summary":
        from src.analysis.prompts import dynamic_signal_summary as prompt
    elif stage == "planning":
        from src.analysis.prompts import open_question_planning as prompt
    elif stage == "answering":
        from src.analysis.prompts import open_question_answering as prompt
    else:
        raise ValueError("canary stage is invalid")
    return (
        prompt.SYSTEM_INSTRUCTION,
        prompt.RESPONSE_SCHEMA,
        prompt.system_instruction_digest(),
        prompt.response_schema_digest(),
    )


def build_canary_request(
    stage_manifest: dict[str, object],
    *,
    policy: CanaryPolicy,
    max_output_tokens: int | None = None,
) -> CanaryRequest:
    if (
        stage_manifest.get("consumer") != policy.consumer
        or stage_manifest.get("stage") != policy.stage
        or stage_manifest.get("input_ceiling") != policy.input_ceiling
        or stage_manifest.get("output_ceiling") != policy.output_ceiling
    ):
        raise ValueError("canary request policy does not match the frozen manifest")
    system_instruction, schema, system_digest, schema_digest = _prompt_contract(policy.stage)
    if (
        stage_manifest.get("system_instruction_sha256") != system_digest
        or stage_manifest.get("response_schema_sha256") != schema_digest
    ):
        raise ValueError("canary request prompt contract digest is invalid")
    max_output_tokens = policy.output_ceiling if max_output_tokens is None else max_output_tokens
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
        or max_output_tokens > policy.output_ceiling
    ):
        raise ValueError("canary max output tokens are invalid")
    thinking = types.ThinkingConfig(thinking_level=policy.thinking_level)
    generation_config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=thinking,
    )
    count_tokens_config = types.CountTokensConfig(
        system_instruction=system_instruction,
        generation_config=types.GenerationConfig(
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_level=policy.thinking_level),
        ),
    )
    return CanaryRequest(
        model=policy.model,
        contents=stage_manifest["contents"],
        count_tokens_config=count_tokens_config,
        generation_config=generation_config,
        input_digest=stage_manifest["input_sha256"],
        system_instruction_digest=system_digest,
        response_schema_digest=schema_digest,
    )


def validate_canary_client(sdk_client: object) -> None:
    api_client = getattr(sdk_client, "_api_client", None)
    http_options = getattr(api_client, "_http_options", None)
    if (
        getattr(sdk_client, "vertexai", None) is not True
        or getattr(api_client, "project", None) != CANARY_PROJECT
        or getattr(api_client, "location", None) != CANARY_LOCATION
        or getattr(http_options, "api_version", None) != CANARY_API_VERSION
    ):
        raise ValueError("canary client configuration is invalid")


__all__ = [
    "CANARY_API_VERSION",
    "CANARY_LOCATION",
    "CANARY_PROJECT",
    "CanaryRequest",
    "build_canary_request",
    "validate_canary_client",
]
