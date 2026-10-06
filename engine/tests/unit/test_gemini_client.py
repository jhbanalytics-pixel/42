"""Unit tests for src/analysis/gemini_client.py.

Tests run against a mocked ``google.genai.Client`` so no live Vertex AI
call is made and no money is spent. The boundary we mock is the SDK
client itself; everything inside ``GeminiClient`` (config assembly,
response parsing, cost calculation) is exercised against real code.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from google.genai import types
from src.analysis.gemini_client import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
    BriefResponse,
    GeminiClient,
    _estimate_cost_usd,
    _resolve_model,
)


def _mock_genai_response(
    text: str = '{"description_rationale": "x"}',
    parsed: dict | None = None,
    prompt_tokens: int = 4500,
    completion_tokens: int = 1800,
):
    """Build a fake genai response object the SDK would return."""
    resp = MagicMock()
    resp.text = text
    resp.parsed = parsed if parsed is not None else {"description_rationale": "x"}
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = completion_tokens
    usage.thoughts_token_count = 0
    resp.usage_metadata = usage
    return resp


def test_estimate_cost_usd_matches_pricing_card():
    """Cost helper prices per model id."""
    # 2.5 Flash: 1M in + 1M out = $0.30 + $2.50 = $2.80.
    assert _estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(
        2.80, rel=1e-6
    )
    # 2.5 Flash realistic single-call shape (~5K in + 2K out) ~= $0.0065.
    assert _estimate_cost_usd("gemini-2.5-flash", 5_000, 2_000) == pytest.approx(0.0065, rel=1e-6)
    # 3.5 Flash: 1M in + 1M out = $1.50 + $9.00 = $10.50.
    assert _estimate_cost_usd("gemini-3.5-flash", 1_000_000, 1_000_000) == pytest.approx(
        10.50, rel=1e-6
    )
    # Unknown id must not under-report: falls back to 3.5 Flash rates.
    assert _estimate_cost_usd("gemini-99", 1_000_000, 0) == pytest.approx(1.50, rel=1e-6)


def test_client_requires_project():
    """No GCP project anywhere = explicit ValueError, no silent default."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="GCP_PROJECT"),
    ):
        GeminiClient(client=MagicMock())


def test_client_defaults_location_to_us_central1(monkeypatch):
    """Location defaults to us-central1 when env not set."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.delenv("VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("GEMINI_LOCATION", raising=False)
    fake_sdk = MagicMock()
    c = GeminiClient(client=fake_sdk)
    assert c.location == DEFAULT_LOCATION
    assert c.project == "test-project"


def test_generate_brief_passes_schema_and_returns_parsed(monkeypatch):
    """Happy path: schema goes into config, response shapes into BriefResponse."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response(
        text='{"description_rationale": "Amapiano leads SA TikTok"}',
        parsed={"description_rationale": "Amapiano leads SA TikTok"},
        prompt_tokens=5_000,
        completion_tokens=2_000,
    )

    client = GeminiClient(client=fake_sdk)
    schema = {"type": "OBJECT", "properties": {"description_rationale": {"type": "STRING"}}}

    result = client.generate_brief(prompt="hello", response_schema=schema)

    assert isinstance(result, BriefResponse)
    assert result.parsed == {"description_rationale": "Amapiano leads SA TikTok"}
    assert result.prompt_tokens == 5_000
    assert result.completion_tokens == 2_000
    assert result.model == DEFAULT_MODEL
    # SDK was actually called with our schema bound to the config.
    call = fake_sdk.models.generate_content.call_args
    config = call.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema == schema


def test_generate_brief_handles_missing_usage_metadata(monkeypatch):
    """Some SDK responses omit usage_metadata. Default token counts to 0."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    resp = MagicMock()
    resp.text = "{}"
    resp.parsed = {}
    resp.usage_metadata = None
    fake_sdk.models.generate_content.return_value = resp

    client = GeminiClient(client=fake_sdk)
    out = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})

    assert out.prompt_tokens == 0
    assert out.completion_tokens == 0
    assert out.usage_metadata_complete is False


def test_generate_brief_normalises_non_dict_parsed(monkeypatch):
    """If the SDK returns a non-dict parsed value, normalise to empty dict."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response(
        text='"oops"', parsed="oops"
    )
    client = GeminiClient(client=fake_sdk)
    out = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    assert out.parsed == {}
    assert out.raw_text == '"oops"'


def _mock_genai_response_raw(text: str, parsed_value: Any) -> MagicMock:
    """Build a fake genai response with full control over parsed (incl. None)."""
    resp = MagicMock()
    resp.text = text
    resp.parsed = parsed_value
    usage = MagicMock()
    usage.prompt_token_count = 1700
    usage.candidates_token_count = 300
    usage.thoughts_token_count = 0
    resp.usage_metadata = usage
    return resp


def test_generate_brief_recovers_from_empty_parsed_via_manual_json(monkeypatch):
    """When result.parsed is empty but raw text is valid JSON, manual parse rescues it.

    Mirrors today's empty-brief failure mode where Vertex returns valid
    JSON in result.text but result.parsed lands as None due to a
    server-side schema validation hiccup.
    """
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    raw_json = (
        '{"description_rationale": "Politics dominate KE TikTok",'
        ' "activation_idea": "Lyria audio drop with creators"}'
    )
    fake_sdk.models.generate_content.return_value = _mock_genai_response_raw(
        text=raw_json, parsed_value=None
    )
    client = GeminiClient(client=fake_sdk)
    out = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    assert out.parsed == {
        "description_rationale": "Politics dominate KE TikTok",
        "activation_idea": "Lyria audio drop with creators",
    }


def test_generate_brief_passes_system_instruction_when_provided(monkeypatch):
    """system_instruction lands on the GenerateContentConfig (Gemini best practice)."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response()
    client = GeminiClient(client=fake_sdk)
    client.generate_brief(
        prompt="task data",
        response_schema={"type": "OBJECT"},
        system_instruction="You are a cultural trends analyst.",
    )
    config = fake_sdk.models.generate_content.call_args.kwargs["config"]
    assert getattr(config, "system_instruction", None) == "You are a cultural trends analyst."


def test_generate_brief_omits_system_instruction_when_not_provided(monkeypatch):
    """No system_instruction key on config when caller does not pass one.

    Tests the legacy call-shape stays clean (no empty string slot).
    """
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response()
    client = GeminiClient(client=fake_sdk)
    client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    config = fake_sdk.models.generate_content.call_args.kwargs["config"]
    # GenerateContentConfig defaults system_instruction to None when not set.
    assert getattr(config, "system_instruction", None) is None


def test_generate_brief_keeps_empty_parsed_when_text_is_not_json(monkeypatch):
    """If raw text is not valid JSON, fall back to empty dict (no crash)."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response_raw(
        text="I cannot answer that.", parsed_value=None
    )
    client = GeminiClient(client=fake_sdk)
    out = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    assert out.parsed == {}
    assert out.raw_text == "I cannot answer that."


def test_resolve_model_defaults_to_flash_25(monkeypatch):
    """Env unset: fall back to 2.5 Flash so local + legacy stays unchanged."""
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert _resolve_model(None) == FALLBACK_MODEL
    assert FALLBACK_MODEL == "gemini-2.5-flash"


def test_resolve_model_reads_env(monkeypatch):
    """GEMINI_MODEL env drives the model (the cron flip)."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    assert _resolve_model(None) == "gemini-3.5-flash"


def test_resolve_model_explicit_arg_wins(monkeypatch):
    """An explicit model arg beats the env (per-call override)."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    assert _resolve_model("gemini-3-pro") == "gemini-3-pro"


def test_generate_brief_uses_env_model_and_sets_thinking_for_gemini3(monkeypatch):
    """3.x model: bounded thinking_config, widened output headroom, model echoed back."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response()
    client = GeminiClient(client=fake_sdk)
    resp = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    call = fake_sdk.models.generate_content.call_args
    assert call.kwargs["model"] == "gemini-3.5-flash"
    config = call.kwargs["config"]
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 1024
    assert config.max_output_tokens == 8192
    assert resp.model == "gemini-3.5-flash"


def test_generate_brief_no_thinking_config_for_flash_25(monkeypatch):
    """2.5 Flash is not a thinking model: no thinking_config slot."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    fake_sdk = MagicMock()
    fake_sdk.models.generate_content.return_value = _mock_genai_response()
    client = GeminiClient(client=fake_sdk)
    client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    config = fake_sdk.models.generate_content.call_args.kwargs["config"]
    assert getattr(config, "thinking_config", None) is None
    assert fake_sdk.models.generate_content.call_args.kwargs["model"] == "gemini-2.5-flash"


def test_gemini_location_env_overrides_vertex_location(monkeypatch):
    """GEMINI_LOCATION wins so 3.x briefs run on global while embeddings stay regional."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_LOCATION", "global")
    c = GeminiClient(client=MagicMock())
    assert c.location == "global"


def test_location_falls_back_to_vertex_when_gemini_location_unset(monkeypatch):
    """No GEMINI_LOCATION: keep using VERTEX_LOCATION (today's behavior)."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("VERTEX_LOCATION", "us-central1")
    monkeypatch.delenv("GEMINI_LOCATION", raising=False)
    c = GeminiClient(client=MagicMock())
    assert c.location == "us-central1"


def test_thinking_tokens_fold_into_completion(monkeypatch):
    """Thinking tokens bill at the output rate; completion must include them."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    resp = MagicMock()
    resp.text = '{"ok": 1}'
    resp.parsed = {"ok": 1}
    usage = MagicMock()
    usage.prompt_token_count = 1000
    usage.candidates_token_count = 500
    usage.thoughts_token_count = 800
    resp.usage_metadata = usage
    fake_sdk.models.generate_content.return_value = resp
    client = GeminiClient(client=fake_sdk)
    out = client.generate_brief(prompt="x", response_schema={"type": "OBJECT"})
    assert out.completion_tokens == 1300
    assert out.usage_metadata_complete is True


def test_manually_constructed_response_defaults_usage_metadata_to_incomplete():
    response = BriefResponse(
        parsed={},
        raw_text="{}",
        prompt_tokens=10,
        completion_tokens=5,
        model="gemini-3.5-flash",
    )

    assert response.usage_metadata_complete is False


@pytest.mark.parametrize("missing_field", ["prompt_token_count", "candidates_token_count"])
def test_generate_brief_marks_required_usage_counter_absence_incomplete(monkeypatch, missing_field):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    usage_values = {"prompt_token_count": 100, "candidates_token_count": 50}
    del usage_values[missing_field]
    response = MagicMock()
    response.text = "{}"
    response.parsed = {}
    response.usage_metadata = SimpleNamespace(**usage_values)
    fake_sdk.models.generate_content.return_value = response

    out = GeminiClient(client=fake_sdk).generate_brief(
        prompt="x", response_schema={"type": "OBJECT"}
    )

    assert out.usage_metadata_complete is False


def test_generate_brief_allows_absent_thought_counter_as_complete_zero(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    response = MagicMock()
    response.text = "{}"
    response.parsed = {}
    response.usage_metadata = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=50,
    )
    fake_sdk.models.generate_content.return_value = response

    out = GeminiClient(client=fake_sdk).generate_brief(
        prompt="x", response_schema={"type": "OBJECT"}
    )

    assert out.prompt_tokens == 100
    assert out.completion_tokens == 50
    assert out.usage_metadata_complete is True


def test_count_tokens_uses_installed_sdk_shape_and_returns_exact_total(monkeypatch):
    """Count preflight uses the installed keyword-only SDK boundary."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    fake_sdk = MagicMock()
    fake_sdk.models.count_tokens.return_value = types.CountTokensResponse(total_tokens=17)

    total = GeminiClient(client=fake_sdk).count_tokens("exact prompt")

    assert total == 17
    fake_sdk.models.count_tokens.assert_called_once_with(
        model="gemini-2.5-flash",
        contents="exact prompt",
    )
    fake_sdk.models.generate_content.assert_not_called()


def test_count_tokens_resolves_explicit_model_without_generation(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    fake_sdk = MagicMock()
    fake_sdk.models.count_tokens.return_value = types.CountTokensResponse(total_tokens=23)

    total = GeminiClient(client=fake_sdk).count_tokens("prompt", model="gemini-3-pro")

    assert total == 23
    fake_sdk.models.count_tokens.assert_called_once_with(
        model="gemini-3-pro",
        contents="prompt",
    )
    fake_sdk.models.generate_content.assert_not_called()


@pytest.mark.parametrize("prompt", ["", None, 123])
def test_count_tokens_rejects_empty_or_malformed_prompt_before_sdk_call(monkeypatch, prompt):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()

    with pytest.raises((TypeError, ValueError), match="prompt"):
        GeminiClient(client=fake_sdk).count_tokens(prompt)  # type: ignore[arg-type]

    fake_sdk.models.count_tokens.assert_not_called()
    fake_sdk.models.generate_content.assert_not_called()


@pytest.mark.parametrize("total", [None, True, -1, 1.5, "17"])
def test_count_tokens_rejects_invalid_sdk_total_without_generation(monkeypatch, total):
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    fake_sdk = MagicMock()
    fake_sdk.models.count_tokens.return_value = SimpleNamespace(total_tokens=total)

    with pytest.raises((TypeError, ValueError), match="total_tokens"):
        GeminiClient(client=fake_sdk).count_tokens("prompt")

    fake_sdk.models.count_tokens.assert_called_once()
    fake_sdk.models.generate_content.assert_not_called()
