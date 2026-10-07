import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from core.agent import writer
from core.agent.tests.test_writer import FakeModel, ctx as ctx, k4_rewrite_draft, support


NARROWED = ["A TikTok post describes amapiano Sundays in Soweto.",
            "A Reddit post says the Soweto amapiano Sunday thing is everywhere this week."]


class RetryModel:
    parallel_calls = 5

    def __init__(self, draft, fail=False):
        self.original = [claim["text"] for claim in draft["claims"]]
        self.rewrites = threading.Barrier(2, timeout=1)
        self.rechecks = threading.Barrier(2, timeout=1)
        self.fail = fail
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append((schema, user))
        usage = {"input_tokens": 100, "output_tokens": 20, "usd": 0.001}
        if schema is writer.K4_REWRITE_SCHEMA:
            self.rewrites.wait()
            index = next(i for i, text in enumerate(self.original) if text in user)
            if self.fail and index == 0:
                error = RuntimeError("failed narrowing retry")
                error.usage = usage
                raise error
            return {"text": NARROWED[index]}, usage
        if any(text in user for text in NARROWED):
            if not self.fail:
                self.rechecks.wait()
            return support("supported"), usage
        return support("partial"), usage


def retry(model, draft, ctx):
    attempted = set()
    result = writer.apply_support(model, draft, ctx, warehouse=SimpleNamespace(),
                                  window=(date(2026, 9, 25), date(2026, 9, 27)), markets=["ZA"],
                                  rewrite_attempted=attempted)
    return result, attempted


def test_narrowing_retries_and_fresh_checks_overlap_and_keep_claim_order(ctx):
    draft = k4_rewrite_draft(ctx)
    model = RetryModel(draft)

    (answer, rows, usage), attempted = retry(model, draft, ctx)

    assert [claim["text"] for claim in answer["claims"]] == NARROWED
    assert [row["claim_id"] for row in rows if row["rule"] == "K4"] == ["c1", "c2"]
    assert attempted == {"c1", "c2"}
    assert usage == {"input_tokens": 600, "output_tokens": 120, "usd": pytest.approx(0.006)}


def test_failed_parallel_retry_keeps_every_billed_sibling_call(ctx):
    draft = k4_rewrite_draft(ctx)
    model = RetryModel(draft, fail=True)

    with pytest.raises(RuntimeError, match="failed narrowing retry") as caught:
        retry(model, draft, ctx)

    assert len(model.calls) == 5
    assert caught.value.usage == {"input_tokens": 500, "output_tokens": 100, "usd": pytest.approx(0.005)}


def test_parallel_retry_reservation_refuses_a_sibling_before_provider_dispatch(monkeypatch, ctx):
    from core.agent import ask
    from core.agent.model_budget import AskModelBudget

    draft = k4_rewrite_draft(ctx)
    bounds = []
    for claim in draft["claims"]:
        recorder = FakeModel({"text": ""})
        records = [ctx.evidence[eid] for eid in claim["evidence_ids"]]
        writer._rewrite_call(recorder, claim, "because", records, "test-model", query_scope={})
        call = recorder.calls[0]
        bounds.append(writer._input_token_upper_bound(call["system"], call["user"], call["schema"]))
    cap = (max(bounds) + writer.K4_REWRITE_MAX_TOKENS) / 1_000_000
    budget = AskModelBudget(cap, cap, price_for_fn=lambda model: {"input": 1, "output": 1},
                            reserve_output_fn=lambda model, tokens: tokens)

    class HeldRetry:
        def __init__(self):
            self.calls = []

        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            assert budget.booked_usd <= cap
            threading.Event().wait(0.2)
            return {"text": ""}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.00012}

    inner = HeldRetry()
    model = ask._StopAwareModel(inner, lambda: False, budget)
    monkeypatch.setattr(writer, "_support_calls_at_once", lambda *args: {
        i: (support("partial"), {}) for i in range(2)})

    with pytest.raises(ask._StopRequested) as caught:
        retry(model, draft, ctx)

    assert caught.value.before_dispatch is True
    assert caught.value.model_budget_reason == "question_model_budget_exhausted"
    assert len(inner.calls) == 1
    assert budget.booked_usd == pytest.approx(0.00012)
    assert budget.booked_usd <= cap
    assert caught.value.usage["usd"] == pytest.approx(0.00012)


def scope_data(user):
    block = user.split("Numerical query scope:\n", 1)[1]
    return json.loads(block.split("<untrusted_content>\n", 1)[1].split("\n</untrusted_content>", 1)[0])


def restored_rows(scope):
    rows = scope["numbers"][0]["matching_rows"]
    if isinstance(rows, list):
        return rows
    return [dict(zip(rows["columns"], values)) for values in rows["rows"]]


def test_full_500_row_scope_fits_without_losing_row_499_or_query_parameters(ctx):
    rows = [{"hashtag": f"tag_{i:03}", "platform": "tiktok", "posts": 5, "creators": 5} for i in range(500)]
    rows[499]["posts"] = 9
    ctx.queries.clear()
    qid, _ = ctx.record_query("SELECT hashtag, platform, posts, creators FROM t WHERE market=@market",
                              {"market": "ZA", "complete_filter": "retained"}, rows, "whole store")
    claim = {"id": "c1", "text": "There were 5 posts.", "label": "observed", "evidence_ids": ["tt_1"],
             "numbers": [{"value": 5, "unit": "posts", "query_id": qid}]}
    scope = writer._claim_query_scope(claim, ctx.queries)
    assert scope["numbers"][0]["matching_rows"] == rows
    model = FakeModel(support("unsupported", "the last row contradicts a universal count"))

    verdict, reason, _ = writer.support_check(model, claim, [ctx.evidence["tt_1"]], queries=ctx.queries)

    assert len(model.calls) == 1
    call = model.calls[0]
    sent = scope_data(call["user"])
    assert restored_rows(sent) == rows
    assert sent["queries"][qid]["params"] == {"market": "ZA", "complete_filter": "retained"}
    assert writer._input_token_upper_bound(call["system"], call["user"], call["schema"]) <= writer.SUPPORT_INPUT_TOKENS
    assert (verdict, reason) == ("unsupported", "the last row contradicts a universal count")
    assert ctx.queries[qid]["rows"] == rows


@pytest.mark.parametrize(("text", "cell", "named"), [
    ("singing", "ng", False), ("ＮＧword", "ＮＧ", False),
    ("si\u0301ng", "ng", False), ("ng\u0301oma", "ng", False),
    ("(NG), #ng; @ng!", "ng", True), ("“São”", "Sa\u0303o", True),
    ("東京語", "東京", False), ("#東京!", "東京", True),
    ("not_ng", "ng", False), ("NG?", "ng", True),
])
def test_names_cell_has_unicode_safe_whole_cell_boundaries(text, cell, named):
    assert writer._names_cell(text, cell) is named
