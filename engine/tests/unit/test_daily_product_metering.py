from contextlib import suppress
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.daily_product_completion import CompletionStore
from src.analysis.open_intelligence.daily_product_io import MeteredModels, ProductBudget

from tests.unit.test_daily_product_completion import Objects


def budget(calls=2):
    return ProductBudget(
        {
            "max_model_calls": calls,
            "max_bytes_billed": 100,
            "max_rows_written": 10,
            "max_credits": 0,
        }
    )


class Models:
    def __init__(self, fail=False, usage=True):
        self.calls = 0
        self.fail = fail
        self.usage = usage

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise TimeoutError("response unknown")
        return SimpleNamespace(
            text='{"ok":true}',
            parsed={"ok": True},
            usage_metadata=SimpleNamespace(
                prompt_token_count=10, candidates_token_count=5, thoughts_token_count=0
            )
            if self.usage
            else None,
        )


def test_every_sdk_attempt_debits_before_dispatch_and_cap_refuses():
    sdk = Models()
    metering = budget(2)
    models = MeteredModels(
        sdk, budget=metering, completion=CompletionStore(Objects()), operation_id="op-1"
    )
    for _ in range(2):
        models.generate_content(model="m", contents="same retry", config={})
    with pytest.raises(ValueError, match="products_model_budget_exhausted"):
        models.generate_content(model="m", contents="third retry", config={})
    assert sdk.calls == metering.model_calls == 2


@pytest.mark.parametrize("failure", ["timeout", "usage"])
def test_unknown_model_effect_poison_survives_a_producer_swallow(failure):
    sdk = Models(fail=failure == "timeout", usage=failure != "usage")
    metering = budget()
    store = CompletionStore(Objects())
    models = MeteredModels(sdk, budget=metering, completion=store, operation_id="op-1")
    with suppress(Exception):
        models.generate_content(model="m", contents="prompt", config={})
    with pytest.raises(ValueError, match="products_effect_unknown"):
        metering.require_complete()
    with pytest.raises(ValueError, match="products_effect_unknown"):
        models.generate_content(model="m", contents="different retry", config={})
    assert sdk.calls == 1
    restarted = MeteredModels(sdk, budget=budget(), completion=store, operation_id="op-1")
    with pytest.raises(ValueError, match="paid_attempt_unknown"):
        restarted.generate_content(model="m", contents="prompt", config={})
    assert sdk.calls == 1


def test_completed_model_result_replays_from_durable_response_without_spend():
    sdk = Models()
    store = CompletionStore(Objects())
    first = MeteredModels(sdk, budget=budget(), completion=store, operation_id="op-1")
    response = first.generate_content(model="m", contents="prompt", config={})
    restarted = MeteredModels(sdk, budget=budget(), completion=store, operation_id="op-1")
    reread = restarted.generate_content(model="m", contents="prompt", config={})
    assert reread.text == response.text
    assert reread.usage_metadata.prompt_token_count == 10
    assert sdk.calls == 1


def test_native_sdk_disables_transport_retries(monkeypatch):
    from google import genai
    from src.analysis.open_intelligence.daily_product_io import native_model_sdk

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return "sdk"

    monkeypatch.setattr(genai, "Client", factory)
    assert native_model_sdk(project="ogilvy-trends-v2", location="global") == "sdk"
    assert captured["http_options"].retry_options.attempts == 1
    assert captured["vertexai"] is True
