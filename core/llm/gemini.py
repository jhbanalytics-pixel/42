"""Gemini on Vertex behind one complete_json seam.

The job's own identity signs the call (google-genai with vertexai=True); there is no API key. Usage comes from
usage_metadata, and thinking tokens are billed as output. An exception raised after a call was billed carries the spend as
both .usage (Ask) and .usd (brief), the two shapes the two callers read. With GEMINI_PRIORITY on, every request goes out as
Vertex Priority PayGo and usage is priced at the priority price (core/llm/provider.py).
"""

from __future__ import annotations

import copy
import json
import os
import re

from core.llm.provider import PRIORITY_HEADER, gemini_priority, price_for, thinking_headroom


def prepare_schema(schema: dict) -> dict:
    """The schema as Gemini takes it: `pattern` is not a supported keyword, so it moves into the description. The limits
    (pattern, maxItems) still hold in code after the call."""
    def walk(node):
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            pattern = node.pop("pattern", None) if isinstance(node.get("pattern"), str) else None
            if pattern:
                node["description"] = (node.get("description", "") + f" Must match {pattern}").strip()
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(copy.deepcopy(schema))


def _without_limits(node):
    """The schema minus the keywords the calling code enforces itself (pattern, maxItems, minItems): the writer, for
    one, renames a claim id off the pattern and drops claims past the cap, so those must not fail the call."""
    if isinstance(node, dict):
        return {k: _without_limits(v) for k, v in node.items() if k not in ("pattern", "maxItems", "minItems")}
    if isinstance(node, list):
        return [_without_limits(v) for v in node]
    return node


def validate(parsed, schema: dict) -> None:
    """Gemini ignores schema keywords it does not support, so check type, required and enum ourselves."""
    try:
        import jsonschema
    except ImportError:  # the jobs image does not carry it; the callers' own checks still run
        return
    jsonschema.validate(parsed, _without_limits(schema))


MEDIA_URI = re.compile(r"(https|gs)://\S+")


def media_parts(media) -> list:
    """The google-genai parts for media: {"uri", "mime_type"} (an https link such as a YouTube watch URL, or gs://)
    by reference, with an optional "end_s" that limits the read to the clip's first end_s seconds, and
    {"bytes", "mime_type"} inline. A malformed part raises ValueError before anything is sent."""
    from google.genai import types

    parts = []
    for part in media:
        if not isinstance(part, dict) or not isinstance(part.get("mime_type"), str) or not part["mime_type"]:
            raise ValueError("a media part is a dict with a mime_type")
        if "uri" in part:
            if not isinstance(part["uri"], str) or not MEDIA_URI.fullmatch(part["uri"]):
                raise ValueError("a media uri must be an https or gs:// address")
            if "end_s" in part:  # read only the clip's first end_s seconds
                end = part["end_s"]
                if isinstance(end, bool) or not isinstance(end, (int, float)) or not 0 < end < 86_400:
                    raise ValueError("end_s must be a number of seconds above 0")
                parts.append(types.Part(file_data=types.FileData(file_uri=part["uri"], mime_type=part["mime_type"]),
                                        video_metadata=types.VideoMetadata(start_offset="0s",
                                                                           end_offset=f"{end:g}s")))
            else:
                parts.append(types.Part.from_uri(file_uri=part["uri"], mime_type=part["mime_type"]))
        elif isinstance(part.get("bytes"), (bytes, bytearray)) and part["bytes"]:
            parts.append(types.Part.from_bytes(data=bytes(part["bytes"]), mime_type=part["mime_type"]))
        else:
            raise ValueError("a media part carries a uri or non-empty bytes")
    return parts


def usage_of(response, model: str) -> dict:
    meta = getattr(response, "usage_metadata", None)
    tokens_in = int(getattr(meta, "prompt_token_count", 0) or 0)  # includes the cached tokens
    cached = min(int(getattr(meta, "cached_content_token_count", 0) or 0), tokens_in)
    # Thinking tokens and tool-use prompt tokens are billed, the first as output and the second as input.
    tokens_in += int(getattr(meta, "tool_use_prompt_token_count", 0) or 0)
    tokens_out = int(getattr(meta, "response_token_count", None) or getattr(meta, "candidates_token_count", 0) or 0)
    tokens_out += int(getattr(meta, "thoughts_token_count", 0) or 0)
    price = price_for(model)
    cached_price = price.get("cached", price["input"])
    return {"input_tokens": tokens_in, "output_tokens": tokens_out,
            "usd": ((tokens_in - cached) * price["input"] + cached * cached_price
                    + tokens_out * price["output"]) / 1_000_000}


class GeminiModel:
    """Structured JSON from Gemini (response_mime_type application/json with a response schema)."""

    def __init__(self, project: str = "ogilvy-trends-v2", region: str = "global", client=None, timeout_s=None,
                 retries: int = 1):
        self.timeout_s = timeout_s
        self.retries = retries
        self.project = project
        self.region = region
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            # One retry by default: a quota error surfaces in seconds, and the brief waits out a busy model itself
            # (core/brief/job.py _Breaker) before its breaker stops the run's calls.
            options = dict(timeout=int(self.timeout_s * 1000) if self.timeout_s else None,
                           retry_options=types.HttpRetryOptions(attempts=self.retries + 1))
            if gemini_priority():
                # Priority PayGo (core/llm/provider.py): every request of this client, a per-call http_options
                # included, since google-genai merges request headers over the client's.
                options["headers"] = dict(PRIORITY_HEADER)
            self._client = genai.Client(vertexai=True, project=self.project, location=self.region,
                                        http_options=types.HttpOptions(**options))
        return self._client

    def config(self, *, system: str, schema: dict | None, max_tokens: int, level: str | None = None, **extra):
        """Thinking counts against max_output_tokens, so the allowance is max_tokens plus THINKING_HEADROOM. No
        temperature, top_p or penalties: 3.8 ignores the first two and errors on penalties."""
        from google.genai import types
        level = (level or os.environ.get("GEMINI_THINKING_LEVEL", "low")).strip().lower()
        if level not in ("low", "medium", "high"):
            raise ValueError(f"GEMINI_THINKING_LEVEL={level!r}: gemini-3.8-flash takes low, medium or high")
        kw = dict(system_instruction=system, max_output_tokens=max_tokens + thinking_headroom(),
                  thinking_config=types.ThinkingConfig(thinking_level=level), **extra)
        if schema is not None:
            kw.update(response_mime_type="application/json", response_json_schema=prepare_schema(schema))
        return types.GenerateContentConfig(**kw)

    def complete_json(self, *, system: str, user: str, schema: dict, model: str, max_tokens: int,
                      request_timeout_s: float | None = None, media: list | None = None) -> tuple[dict, dict]:
        """media, when given, goes before the user text as parts (media_parts); its tokens come back in the usage
        and are billed at the model's input price like any other prompt token. Without media, contents is the user
        text alone, as before."""
        price_for(model)  # an unpriced model fails before it spends anything
        contents = [*media_parts(media), user] if media else user
        extra = {}
        if request_timeout_s is not None:
            from google.genai import types
            timeout_ms = int(request_timeout_s * 1000)
            if timeout_ms < 1:
                raise ValueError("request_timeout_s must be at least one millisecond")
            extra["http_options"] = types.HttpOptions(
                timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
        response = self.client.models.generate_content(
            model=model, contents=contents,
            config=self.config(system=system, schema=schema, max_tokens=max_tokens, **extra))
        usage = usage_of(response, model)
        try:
            candidate = (getattr(response, "candidates", None) or [None])[0]
            reason = str(getattr(getattr(candidate, "finish_reason", None), "name", getattr(candidate, "finish_reason", "")))
            if "MAX_TOKENS" in reason:
                raise RuntimeError(f"{model} hit max_tokens={max_tokens}; the JSON is truncated")
            if "PROHIBITED" in reason or "SAFETY" in reason:
                raise RuntimeError(f"{model} blocked the request or reply (finish reason {reason})")
            text = response.text
            if not text:
                raise RuntimeError(f"{model} returned no text (finish reason {reason or 'unknown'})")
            parsed = json.loads(text)
            validate(parsed, schema)
            return parsed, usage
        except Exception as exc:
            exc.usage = usage  # billed even though the output is unusable
            exc.usd = usage["usd"]
            raise
