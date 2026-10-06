"""Thin Vertex AI Gemini wrapper for the Phase 2 analysis layer.

Uses the ``google-genai`` SDK (the unified client that supersedes
``vertexai.generative_models``, which retires June 2026). Authenticates
to Vertex AI on the project's GCP account via Application Default
Credentials so the cloud cron's service account works without further
configuration.

Public surface
==============

``GeminiClient.generate_brief(prompt, response_schema, model)`` returns
a ``BriefResponse`` carrying parsed JSON + token counts so callers can
log cost per call. The schema is enforced server-side via
``response_mime_type='application/json'`` + ``response_schema``, so the
return value is guaranteed-shaped or the call raises.

Failure mode is fail-loud: a Vertex outage propagates as the underlying
exception. The pipeline orchestrator decides whether to retry or skip
that brief; this module does not silently swallow.

Costs
=====

The live model is selected by the ``GEMINI_MODEL`` env var (default
``gemini-2.5-flash``). At ``gemini-3.5-flash`` ($1.50 per 1M input,
$9.00 per 1M output including thinking tokens) one brief runs roughly
3.4K input + bounded thinking + a 9-field JSON output. ``_estimate_cost_usd``
prices per model so the per-call log line tracks whichever model is live.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-central1"

# Per-1M-token (input, output) USD rates. Source: Google Cloud Vertex AI
# pricing, May 2026. Output rates include billed thinking tokens on the
# Gemini 3.x thinking models.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3-pro": (2.00, 12.00),
}
# Unknown id: assume 3.5 Flash rates so the cost ledger never under-reports.
_FALLBACK_PRICING: tuple[float, float] = (1.50, 9.00)


def _resolve_model(model: str | None) -> str:
    """Resolve the model id: explicit arg > GEMINI_MODEL env > fallback.

    Resolving at call time (not import) lets the cron's workflow-level
    GEMINI_MODEL env pick the model with no code change, and lets tests
    monkeypatch the env per case.
    """
    return model or os.environ.get("GEMINI_MODEL") or FALLBACK_MODEL


# Back-compat alias; callers/tests import DEFAULT_MODEL by name.
DEFAULT_MODEL = FALLBACK_MODEL


@dataclass(frozen=True)
class BriefResponse:
    """Single Gemini response shaped for downstream consumers.

    ``parsed`` is the JSON payload as a Python dict, validated against
    the ``response_schema`` passed to ``generate_brief``. ``raw_text``
    is the verbatim model output before parsing, retained for debugging
    when JSON validation fails on the caller side. ``prompt_tokens`` +
    ``completion_tokens`` feed the cost ledger; ``model`` is the exact
    model identifier the call ran against (useful when comparing
    different Gemini versions). ``usage_metadata_complete`` is true only
    when the SDK response supplied both required token counters.
    """

    parsed: dict[str, Any]
    raw_text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    usage_metadata_complete: bool = False


class GeminiClient:
    """Vertex AI Gemini wrapper bound to the project's GCP account.

    Project + location come from the environment (``GCP_PROJECT`` +
    ``VERTEX_LOCATION``) so the same code works locally with ADC and
    on the cloud cron with the service account key. Override at
    construction time only when running against a different project
    (e.g. integration tests).
    """

    def __init__(
        self,
        project: str | None = None,
        location: str | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self.project = project or os.environ.get("GCP_PROJECT")
        # GEMINI_LOCATION lets briefs run in a different region than embeddings
        # (VERTEX_LOCATION). Gemini 3.x is global-endpoint-only on Vertex, so the
        # cron sets GEMINI_LOCATION=global alongside GEMINI_MODEL=gemini-3.5-flash
        # while the embedding classifier stays on us-central1.
        self.location = (
            location
            or os.environ.get("GEMINI_LOCATION")
            or os.environ.get("VERTEX_LOCATION", DEFAULT_LOCATION)
        )
        if not self.project:
            raise ValueError(
                "GCP_PROJECT not set; cannot initialise Vertex AI client. "
                "Set GCP_PROJECT in env or pass project= explicitly."
            )
        # Allow injection of a pre-built client for tests so we can mock
        # the SDK boundary without monkey-patching.
        self._client = client or genai.Client(
            vertexai=True,
            project=self.project,
            location=self.location,
        )

    def count_tokens(self, prompt: str, model: str | None = None) -> int:
        """Count one exact prompt through the installed SDK boundary."""
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if not prompt:
            raise ValueError("prompt must be nonempty")
        result = self._client.models.count_tokens(
            model=_resolve_model(model),
            contents=prompt,
        )
        total_tokens = getattr(result, "total_tokens", None)
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
            raise TypeError("count_tokens total_tokens must be an integer")
        if total_tokens < 0:
            raise ValueError("count_tokens total_tokens must be nonnegative")
        return total_tokens

    def generate_brief(
        self,
        prompt: str,
        response_schema: dict[str, Any],
        model: str | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 8192,
        system_instruction: str | None = None,
        thinking_budget: int = 1024,
    ) -> BriefResponse:
        """Send one structured-JSON request to Vertex Gemini.

        Forces ``application/json`` output bound to ``response_schema``
        so the model returns parseable JSON or fails. Returns a
        BriefResponse with parsed dict + token counts for cost tracking.
        Raises whatever the SDK raises on transient errors; the caller
        decides retry policy.

        ``system_instruction`` is the Vertex AI best-practice slot for
        the model's persona + role + unchanging ground rules. Keeping
        these out of the user-content message (where they would mix with
        task data) makes the model more robust to large or adversarial
        inputs and keeps token accounting cleaner. See Google's prompt
        engineering guide for Gemini: prompts split into a system layer
        (who the model is) and a user layer (what data + task) score
        higher on benchmarks than monolithic prompts.

        ``max_output_tokens`` defaults to 8192. Gemini 3.x are thinking
        models: the bounded thinking budget is billed as output tokens, so
        a tight ceiling starves the visible JSON into an empty or truncated
        response (the regression-3 empty-summary failure mode). 8192 leaves
        headroom above the thinking budget for the full structured payload.
        The brief carries eleven required fields (headline,
        description_rationale, activation_idea, visual_anchor,
        nano_banana_prompt, lyria_prompt, key_metrics, platforms,
        sentiment_summary, status_tag, risk_flags); the daily_summary call
        passes 8192 explicitly for the same reason (see
        generate_daily_summary.py).
        """
        resolved_model = _resolve_model(model)
        config_kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": response_schema,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        # Gemini 3.x are thinking models: hidden thinking tokens are billed as
        # output and, against a tight max_output_tokens, can starve the visible
        # JSON into an empty/truncated response (schema-collapse). Bound thinking
        # and rely on the widened output headroom.
        if resolved_model.startswith("gemini-3"):
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
        config = types.GenerateContentConfig(**config_kwargs)

        result = self._client.models.generate_content(
            model=resolved_model,
            contents=prompt,
            config=config,
        )

        raw_text = result.text or ""
        parsed = result.parsed if hasattr(result, "parsed") and result.parsed else {}
        if not parsed and raw_text:
            # The SDK's result.parsed sometimes lands empty when the
            # server-side schema validator marginally rejects the
            # response (e.g. an extra field, a stray whitespace) even
            # though the raw text itself is valid JSON we can use. Try
            # a manual parse before giving up; this recovers briefs that
            # would otherwise persist as empty rows.
            try:
                candidate = json.loads(raw_text)
                if isinstance(candidate, dict):
                    parsed = candidate
            except (json.JSONDecodeError, ValueError):
                pass
        if not isinstance(parsed, dict):
            # Some SDK versions return the parsed payload as the model object;
            # normalise to dict so downstream is uniform.
            try:
                parsed = dict(parsed)
            except (TypeError, ValueError):
                parsed = {}

        usage = getattr(result, "usage_metadata", None)
        prompt_token_count = getattr(usage, "prompt_token_count", None)
        candidates_token_count = getattr(usage, "candidates_token_count", None)
        usage_metadata_complete = (
            usage is not None
            and prompt_token_count is not None
            and candidates_token_count is not None
        )
        prompt_tokens = int(prompt_token_count or 0)
        # Thinking tokens (Gemini 3.x) bill at the output rate but land in a
        # separate counter; fold them into completion so the cost ledger is true.
        completion_tokens = int(candidates_token_count or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0
        )

        logger.info(
            "Gemini brief generated: model=%s prompt_tokens=%d completion_tokens=%d "
            "estimated_cost_usd=%.5f",
            resolved_model,
            prompt_tokens,
            completion_tokens,
            _estimate_cost_usd(resolved_model, prompt_tokens, completion_tokens),
        )

        return BriefResponse(
            parsed=parsed,
            raw_text=raw_text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=resolved_model,
            usage_metadata_complete=usage_metadata_complete,
        )


def _estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Per-call cost estimate at the live model's rates.

    Used for the per-call log line that morning-check + the cost watchdog
    read. Output tokens on Gemini 3.x include billed thinking tokens.
    """
    in_rate, out_rate = _MODEL_PRICING.get(model, _FALLBACK_PRICING)
    return (prompt_tokens / 1_000_000.0) * in_rate + (completion_tokens / 1_000_000.0) * out_rate


__all__ = ["DEFAULT_LOCATION", "DEFAULT_MODEL", "FALLBACK_MODEL", "BriefResponse", "GeminiClient"]
