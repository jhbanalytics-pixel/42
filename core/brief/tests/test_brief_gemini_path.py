import json
import re
from types import SimpleNamespace

import pytest
from google import genai
from google.genai import types

from core.brief import explain, job
from core.brief.tests import test_brief_job as job_tests
from core.llm.provider import GEMINI_LIST_PRICES, default_model


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        properties = config.response_json_schema["properties"]
        if "explanation" in properties:
            ids = re.findall(r'post \{"id": "([^"]+)"', contents)
            output = job_tests.draft(ids)
        elif "ruled_out" in properties:
            output = {"non_cultural_explanation": "a paid campaign", "ruled_out": True, "news_driven": False,
                      "scheduled_event": False, "local_reaction": False, "local_why_now": True,
                      "reason": "The local posts support the timing."}
        elif "verdict" in properties:
            output = {"verdict": "supported", "reason": "The cited posts support this claim."}
        else:
            output = {"non_cultural_explanation": "a paid campaign", "ruled_out": True,
                      "reason": "The posts come from unrelated creators."}
        return SimpleNamespace(
            text=json.dumps(output),
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
            usage_metadata=SimpleNamespace(prompt_token_count=120, cached_content_token_count=20,
                                           tool_use_prompt_token_count=5, response_token_count=30,
                                           thoughts_token_count=40),
        )


def fake_sdk_client(sdk):
    async def generate_content(**kwargs):
        return sdk.generate_content(**kwargs)

    async def aclose():
        pass

    return SimpleNamespace(models=sdk, aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content),
                                                          aclose=aclose), close=lambda: None)


def _world():
    con = job_tests.world(n=0, markets=("ZA",))
    job_tests.add_item(con, "ZA", "za1", 0.95, posts=3)
    return con


def _market_scope(*args, **kwargs):
    return {"market_scope": "market", "market_posts7": 3, "total_posts7": 3, "market_share7": 1}


def _gemini_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FAST_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_INPUT_PER_M", raising=False)
    monkeypatch.delenv("GEMINI_PRICE_OUTPUT_PER_M", raising=False)
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low")


def test_brief_factory_runs_the_gemini_sdk_path_through_checks_payload_and_cost(monkeypatch):
    _gemini_env(monkeypatch)
    monkeypatch.setattr(job, "read_market_scope", _market_scope, raising=False)
    sdk = FakeModels()
    client_options = []
    monkeypatch.setattr(genai, "Client", lambda **kw: client_options.append(kw) or fake_sdk_client(sdk))
    monkeypatch.setattr(job, "model_daily_usd", lambda: 1.0)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1.0)

    model = job.brief_model()
    result = job_tests.brief(_world(), model=model, workers=1)

    assert len(sdk.calls) == 6
    assert ["explanation" in c["config"].response_json_schema["properties"] for c in sdk.calls] == [True] + [False] * 5
    assert ["verdict" in c["config"].response_json_schema["properties"] for c in sdk.calls] == [False] * 1 + [True] * 4 + [False]
    assert "ruled_out" in sdk.calls[-1]["config"].response_json_schema["properties"]
    assert "local_why_now" in sdk.calls[-1]["config"].response_json_schema["properties"]
    assert all(c["model"] == default_model() for c in sdk.calls)
    assert all(isinstance(c["config"], types.GenerateContentConfig) for c in sdk.calls)
    assert [c["config"].max_output_tokens for c in sdk.calls] == [
        explain.WRITER_MAX_TOKENS + 2000,
        *([explain.SUPPORT_MAX_TOKENS + 2000] * 4),
        explain.CRITIC_MAX_TOKENS + 2000,
    ]
    assert all(c["config"].thinking_config.thinking_level.name == "LOW" for c in sdk.calls)
    assert len(client_options) == 1
    assert client_options[0]["vertexai"] is True
    assert client_options[0]["project"] == job.PROJECT
    assert client_options[0]["http_options"].timeout == job.GEMINI_TIMEOUT_S * 1000

    price = GEMINI_LIST_PRICES[default_model()]
    usd_per_call = ((120 + 5 - 20) * price["input"] + 20 * price["cached"]
                    + (30 + 40) * price["output"]) / 1_000_000
    expected_usd = round(usd_per_call * len(sdk.calls), 6)
    assert result.counts["model_usd"] == pytest.approx(expected_usd)
    assert result.chain.finished[0]["counts"]["model_usd"] == pytest.approx(expected_usd)

    payload = job_tests.payload(result, "ZA")
    [card] = payload["cards"]
    assert card["explained"] is True
    assert card["explanation_status"] == "explained"
    assert len(card["claims"]) == 3
    specificity = card["specificity"]
    assert specificity["status"] == "pass"
    assert len(set(specificity["local_evidence_ids"])) >= 2
    assert specificity["why_now"] == card["explanation"]
    quote = specificity["quote"]
    assert quote["text"] == "Dancing to the new sound"
    assert quote["evidence_id"] in specificity["local_evidence_ids"]
    evidence = {row["id"]: row for row in card["evidence"]}
    assert quote["text"] in evidence[quote["evidence_id"]]["quote_text"]
    rules = {row["rule"] for row in job_tests.checks(result)}
    assert "K4" in rules and "critic" in rules
    assert next(row for row in job_tests.checks(result) if row["rule"] == "critic")["verdict"] == "pass"


def test_brief_cap_reserves_thinking_headroom_before_the_first_gemini_sdk_call(monkeypatch):
    _gemini_env(monkeypatch)
    monkeypatch.setattr(job, "read_market_scope", _market_scope, raising=False)
    con = _world()
    client = job_tests.Client(con)
    candidate = job._candidates(client, job_tests.D, build_ctx=job_tests.FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in job_tests.MARKETS}, core="core",
                                agent="agent")["ZA"][0]
    local_sources = [row for row in candidate["pack"]["evidence"]
                     if row.get("market") == "ZA" or row.get("source_market") == "ZA"]
    assert len({row["id"] for row in local_sources}) >= 2
    assert any("Dancing to the new sound" in row.get("quote_text", "") for row in local_sources)
    start, end = job._window(job_tests.D, "ZA")
    user = explain._writer_user(candidate["row"], candidate["pack"], "ZA", start, end)
    model_id = default_model()
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "0")
    without_headroom = explain._estimate_usd(explain.WRITER_SYSTEM, user, explain.WRITER_MAX_TOKENS, model_id)
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")
    with_headroom = explain._estimate_usd(explain.WRITER_SYSTEM, user, explain.WRITER_MAX_TOKENS, model_id)
    assert without_headroom < with_headroom
    cap = (without_headroom + with_headroom) / 2

    estimate_calls = []
    estimate_usd = explain._estimate_usd

    def record_estimate(system, user, max_tokens, model_id):
        estimate = estimate_usd(system, user, max_tokens, model_id)
        estimate_calls.append({"estimate": estimate, "max_tokens": max_tokens})
        return estimate

    monkeypatch.setattr(explain, "_estimate_usd", record_estimate)
    sdk = FakeModels()
    monkeypatch.setattr(genai, "Client", lambda **kw: fake_sdk_client(sdk))
    monkeypatch.setattr(job, "model_daily_usd", lambda: cap)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: cap)
    result = job_tests.brief(con, model=job.brief_model(), workers=1)

    assert len(estimate_calls) == 1
    assert estimate_calls[0]["max_tokens"] == explain.WRITER_MAX_TOKENS
    assert estimate_calls[0]["estimate"] == pytest.approx(with_headroom)
    assert estimate_calls[0]["estimate"] > cap
    assert sdk.calls == []
    payload = job_tests.payload(result, "ZA")
    assert payload["cards"] == [] and payload["more"] == []
    assert payload["held_back"]["count"] == 1
    [held] = payload["held_back"]["items"]
    assert held["item_id"] == "za1"
    assert held["rule"] == "G10"
    assert held["reason"] == "explanation_failed"
    assert len(held["evidence"]) == 3
    assert "Dancing to the new sound" in " ".join(row["quote_text"] for row in held["evidence"])
    assert result.counts["model_usd"] == 0
