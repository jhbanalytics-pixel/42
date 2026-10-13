from unittest.mock import patch

import pytest

from core.llm import gemini, provider
from core.llm.tests.test_llm import FakeClient, response
from core.understand import enrich
from core.understand.tests.test_enrich import good_output


def test_validated_price_is_snapshotted_before_dispatch_and_not_read_again():
    client = FakeClient(response())
    with patch.object(gemini, "price_for", side_effect=[{"input": 1, "output": 2},
                                                       provider.PriceUnset("late price")]) as prices:
        out, usage = gemini.GeminiModel(client=client).complete_json(
            system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert out == {"ok": True} and len(client.calls) == prices.call_count == 1
    assert usage["usd"] == pytest.approx((100 + 50 * 2) / 1e6)


@pytest.mark.parametrize("field", ["prompt_token_count", "response_token_count", "candidates_token_count",
                                   "cached_content_token_count", "thoughts_token_count", "tool_use_prompt_token_count"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, -4, 1.5, "invalid"])
def test_invalid_reported_counters_preserve_content_and_mark_usage_unknown(field, bad):
    reply = response()
    setattr(reply.usage_metadata, field, bad)
    client = FakeClient(reply)
    out, usage = gemini.GeminiModel(client=client).complete_json(
        system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert out == {"ok": True} and len(client.calls) == 1
    assert usage["usage_unknown"] and usage["input_tokens"] is usage["output_tokens"] is usage["usd"] is None


def test_zero_primary_output_is_not_replaced_by_legacy_candidates():
    reply = response(pin=0, pout=0, think=0)
    reply.usage_metadata.candidates_token_count = 99
    assert gemini.usage_of(reply, "gemini-3.8-flash")["output_tokens"] == 0


def test_post_dispatch_accounting_exception_is_annotated_with_unknown_reservation():
    client = FakeClient(response())
    with patch.object(gemini, "usage_of", side_effect=ValueError("unreadable metadata")):
        with pytest.raises(ValueError) as error:
            gemini.GeminiModel(client=client).complete_json(
                system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert len(client.calls) == 1
    assert error.value.usage is error.value.usd is None and error.value.reserve_model_estimate


@pytest.mark.parametrize("deadline", [None, 10.0])
def test_invalid_usage_is_reserved_once_by_both_sync_runner_paths(monkeypatch, deadline):
    import json

    monkeypatch.setattr(enrich, "GEMINI_SYNC_MAX_INFLIGHT", 1)
    reply = response(text=json.dumps(good_output()), pin=float("nan"))
    client = FakeClient(reply)
    post = {"post_id": "p", "content": "dance"}
    result = enrich.SyncRunner(gemini.GeminiModel(client=client)).run(
        [post, {"post_id": "later", "content": "dance"}], deadline=deadline, monotonic=lambda: 0.0)
    assert len(client.calls) == len(result.outputs) == 1
    assert result.outputs[0]["output"] == good_output()
    assert result.unknown_spend_usd == enrich.est_usd(post, batched=False) > 0
    assert result.skipped == ["later"]


@pytest.mark.parametrize("deadline", [None, 10.0])
def test_post_dispatch_accounting_error_books_one_estimate_in_each_sync_runner_path(monkeypatch, deadline):
    monkeypatch.setattr(enrich, "GEMINI_SYNC_MAX_INFLIGHT", 1)
    client = FakeClient(response())
    post = {"post_id": "p", "content": "dance"}
    with patch.object(gemini, "usage_of", side_effect=ValueError("metadata decoder failed")):
        result = enrich.SyncRunner(gemini.GeminiModel(client=client)).run(
            [post, {"post_id": "later", "content": "dance"}], deadline=deadline, monotonic=lambda: 0.0)
    assert len(client.calls) == len(result.outputs) == 1 and result.skipped == ["later"]
    counts = {"enriched": 0, "failed": 0, "skipped": 0}
    bookings = []

    def no_insert(*args):
        raise AssertionError("failed model output must not be persisted")

    enrich._book(no_insert, result, [post, {"post_id": "later", "content": "dance"}], counts, set(), batched=False,
                 spend=lambda amount, what: bookings.append(amount) if amount else None)
    assert bookings == [enrich.est_usd(post, batched=False)]
    assert counts["unknown_spend"] and counts["failed"] == 1 and counts["enriched"] == 0


def test_in_place_price_mutation_after_dispatch_does_not_change_the_snapshot():
    quote = {"input": 1, "output": 2}
    client = FakeClient(response())
    generate = client.models.generate_content

    def mutate_quote(**kwargs):
        quote.update(input=100, output=200)
        return generate(**kwargs)

    client.models.generate_content = mutate_quote
    with patch.object(gemini, "price_for", return_value=quote):
        _, usage = gemini.GeminiModel(client=client).complete_json(
            system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert usage["usd"] == pytest.approx((100 + 50 * 2) / 1e6)
