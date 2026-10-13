from types import SimpleNamespace

import pytest

from core.llm import provider
from core.llm.gemini import GeminiModel


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-custom"])
@pytest.mark.parametrize("key", ["GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "invalid", ""])
def test_explicit_invalid_price_refuses_before_dispatch(monkeypatch, model, key, value):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "2")
    monkeypatch.setenv(key, value)
    calls = []
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: calls.append(kwargs)))
    with pytest.raises(provider.PriceUnset):
        GeminiModel(client=client).complete_json(system="s", user="u", schema={}, model=model, max_tokens=10)
    assert calls == []


@pytest.mark.parametrize("key", ["GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M"])
def test_partial_explicit_price_refuses_instead_of_falling_back(monkeypatch, key):
    for name in ("GEMINI_PRICE_INPUT_PER_M", "GEMINI_PRICE_OUTPUT_PER_M", "GEMINI_PRIORITY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, "1")
    with pytest.raises(provider.PriceUnset):
        provider.price_for("gemini-3.8-flash")


def test_complete_explicit_zero_prices_remain_valid(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "0")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "0")
    monkeypatch.delenv("GEMINI_PRIORITY", raising=False)
    assert provider.price_for("gemini-custom") == {"input": 0, "output": 0, "cached": 0}
