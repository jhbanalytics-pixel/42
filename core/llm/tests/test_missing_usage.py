from types import SimpleNamespace as NS

import pytest

from core.llm.gemini import GeminiModel
from core.llm.tests.test_llm import FakeClient, response


@pytest.mark.parametrize("metadata", [None, NS(prompt_token_count=None, candidates_token_count=5),
                                       NS(prompt_token_count=5, candidates_token_count=None)])
def test_successful_content_is_retained_with_explicit_unknown_usage(metadata):
    reply = response()
    reply.usage_metadata = metadata
    out, usage = GeminiModel(client=FakeClient(reply)).complete_json(
        system="s", user="u", schema={"type": "object"}, model="gemini-3.8-flash", max_tokens=10)
    assert out == {"ok": True}
    assert usage["usage_unknown"] is True
    assert usage["input_tokens"] is usage["output_tokens"] is usage["usd"] is None


def test_unusable_output_with_missing_usage_retains_reservation_marker():
    reply = response(text="bad JSON")
    reply.usage_metadata = None
    with pytest.raises(ValueError) as error:
        GeminiModel(client=FakeClient(reply)).complete_json(
            system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert error.value.usage is None and error.value.usd is None
    assert error.value.reserve_model_estimate is True


def test_reported_zero_usage_is_distinct_from_missing_usage():
    reply = response(pin=0, pout=0, think=0)
    _, usage = GeminiModel(client=FakeClient(reply)).complete_json(
        system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert usage["input_tokens"] == usage["output_tokens"] == usage["usd"] == 0
    assert not usage.get("usage_unknown")
