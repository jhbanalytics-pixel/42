"""Decode already-captured provider responses without invoking generation."""

import copy
import json
import math

_COUNTERS = {
    "prompt_token_count": "promptTokenCount",
    "candidates_token_count": "candidatesTokenCount",
    "thoughts_token_count": "thoughtsTokenCount",
    "total_token_count": "totalTokenCount",
    "tool_use_prompt_token_count": "toolUsePromptTokenCount",
}


def _tree(value):
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("response_invalid")
        return
    if type(value) is str:
        value.encode("utf-8")
        return
    if type(value) is list:
        for item in value:
            _tree(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("response_invalid")
            key.encode("utf-8")
            _tree(item)
        return
    raise ValueError("response_invalid")


def _json_object(text, code):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(code)
            result[key] = value
        return result

    def integer(value):
        if len(value.lstrip("-")) > 4300:
            raise ValueError(code)
        return int(value)

    try:
        if type(text) is not str:
            raise ValueError(code)
        text.encode("utf-8")
        value = json.loads(text, object_pairs_hook=unique, parse_int=integer)
        _tree(value)
        if type(value) is not dict:
            raise ValueError(code)
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError(code) from error


def decode_question_response(raw_sdk_response: dict) -> dict:
    try:
        if type(raw_sdk_response) is not dict:
            raise ValueError("response_invalid")
        _tree(raw_sdk_response)
        http = raw_sdk_response.get("sdk_http_response")
        if http is not None and type(http) is not dict:
            raise ValueError("response_invalid")
        raw_mode = http is not None and http.get("body") is not None
        if raw_mode:
            if any(
                raw_sdk_response.get(key) is not None
                for key in ("model_version", "usage_metadata", "candidates")
            ):
                raise ValueError("response_invalid")
            value = _json_object(http["body"], "response_invalid")
            if "model_version" in value or "usage_metadata" in value:
                raise ValueError("response_invalid")
            model = value.get("modelVersion")
            usage = value.get("usageMetadata")
        else:
            value = raw_sdk_response
            model = value.get("model_version")
            usage = value.get("usage_metadata")
        if model is not None and type(model) is not str:
            raise ValueError("response_invalid")
        if usage is not None:
            if type(usage) is not dict:
                raise ValueError("response_invalid")
            if raw_mode and any(key in usage for key in _COUNTERS):
                raise ValueError("response_invalid")
            usage = {
                key: usage[alias if raw_mode else key]
                for key, alias in _COUNTERS.items()
                if (alias if raw_mode else key) in usage
            }
        return copy.deepcopy(
            {
                "model_version": model,
                "usage_metadata": usage,
                "candidates": value.get("candidates"),
            }
        )
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("response_invalid") from error


def question_response_text(raw_sdk_response: dict) -> str:
    value = decode_question_response(raw_sdk_response)
    candidates = value["candidates"]
    code = "semantic_output_invalid"
    if type(candidates) is not list or len(candidates) != 1 or type(candidates[0]) is not dict:
        raise ValueError(code)
    finish_fields = {"finish_reason", "finishReason"} & set(candidates[0])
    if len(finish_fields) != 1 or candidates[0][next(iter(finish_fields))] != "STOP":
        raise ValueError(code)
    content = candidates[0].get("content")
    if type(content) is not dict or content.get("role") not in (None, "model"):
        raise ValueError(code)
    parts = content.get("parts")
    if type(parts) is not list or not parts:
        raise ValueError(code)
    texts = []
    for part in parts:
        if type(part) is not dict or type(part.get("text")) is not str:
            raise ValueError(code)
        if "thought_signature" in part and "thoughtSignature" in part:
            raise ValueError(code)
        for key, item in part.items():
            if item is None:
                continue
            if key == "text":
                continue
            if key == "thought" and type(item) is bool and item is False:
                continue
            if key in ("thought_signature", "thoughtSignature") and type(item) is str:
                continue
            raise ValueError(code)
        texts.append(part["text"])
    text = "".join(texts)
    _json_object(text, code)
    return text
