from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest

from core.agent import ask, gemini_research
from core.agent.tools import sql_query, warehouse
from core.agent.model_budget import AskModelBudget, BudgetRefused
from core.agent.writer import WRITER_SCHEMA
from core.agent.tests.test_ask import CONSERVATIVE_NOTICE, NOW, SEARCH_EMBED_USD, FakeModel, Harness, make_research
from core.llm.gemini import GeminiModel


def make_budget(hold=0.003, research=0.003):
    return AskModelBudget(
        hold,
        research,
        price_for_fn=lambda model: {"input": 1.0, "output": 1.0},
        reserve_output_fn=lambda model, tokens: tokens,
    )


def reserve(budget, *, research=True, input_bound=1000, max_output=1000):
    return budget.reserve("gemini-test", input_bound, max_output, research=research)


def configure_gemini(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1.5")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "7.5")
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")
    monkeypatch.setattr(ask, "MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(ask, "FALLBACK_MODEL", "gemini-3.8-flash")


class RecordingModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.usages = []

    def complete_json(self, **kwargs):
        result, usage = super().complete_json(**kwargs)
        self.usages.append(usage)
        return result, usage


def test_unknown_usage_books_the_full_reserve_and_blocks_a_second_dispatch():
    budget = make_budget(hold=0.003, research=0.003)
    dispatches = 0

    def dispatch():
        nonlocal dispatches
        ticket = reserve(budget)
        dispatches += 1
        budget.settle(ticket, None)

    dispatch()
    with pytest.raises(BudgetRefused):
        dispatch()

    assert dispatches == 1
    assert budget.booked_usd == 0.002
    assert budget.stopped


def test_partial_usage_books_the_full_reserve_and_stops():
    budget = make_budget()
    ticket = reserve(budget)

    assert not budget.settle(ticket, {"input_tokens": 100, "usd": 0.0001})

    assert budget.booked_usd == 0.002
    assert budget.stopped


def test_reported_cost_above_the_serialized_bound_is_never_underbooked():
    budget = make_budget()
    ticket = reserve(budget)

    assert not budget.settle(ticket, {"input_tokens": 1500, "output_tokens": 100, "usd": 0.004})

    assert budget.booked_usd == 0.004
    assert budget.stopped


def test_known_usage_releases_the_unused_reserve_for_a_later_call():
    budget = make_budget(hold=0.003, research=0.003)
    first = reserve(budget)

    assert budget.settle(first, {"input_tokens": 100, "output_tokens": 100, "usd": 0.0002})
    second = reserve(budget)

    assert budget.settle(second, {"input_tokens": 100, "output_tokens": 100, "usd": 0.0002})
    assert budget.booked_usd == 0.0004


def test_call_error_retains_its_full_ceiling_and_stops_dispatch():
    budget = make_budget()
    ticket = reserve(budget)

    assert budget.fail(ticket) == 0.002
    with pytest.raises(BudgetRefused):
        reserve(budget)

    assert budget.booked_usd == 0.002
    assert budget.stopped


def test_billed_structured_call_error_uses_complete_usage_without_a_full_reserve():
    budget = make_budget()
    usage = {"input_tokens": 10, "output_tokens": 5, "usd": 0.000015}

    class BilledFailure:
        def complete_json(self, **kwargs):
            error = RuntimeError("structured result could not be parsed")
            error.usage = usage
            raise error

    model = ask._StopAwareModel(BilledFailure(), lambda: False, budget)
    with pytest.raises(RuntimeError, match="could not be parsed"):
        model.complete_json(system="x", user="y", schema={}, model="gemini-test", max_tokens=10)

    assert budget.booked_usd == usage["usd"]
    assert budget.conservative_usd == 0


def test_research_subbudget_refuses_a_call_even_when_the_question_hold_has_room():
    budget = make_budget(hold=0.01, research=0.001)

    with pytest.raises(BudgetRefused):
        reserve(budget, research=True)

    assert budget.booked_usd == 0
    assert budget.research_exhausted
    assert not budget.stopped
    writer_call = reserve(budget, research=False)
    assert budget.settle(writer_call, {"input_tokens": 100, "output_tokens": 100, "usd": 0.0002})


def test_research_ceiling_prevents_a_second_call_from_crossing_two_dollars():
    budget = AskModelBudget(
        hold_usd=3.0,
        research_usd=2.0,
        price_for_fn=lambda model: {"input": 1.5, "output": 7.5},
        reserve_output_fn=lambda model, tokens: tokens,
    )
    dispatches = 0
    first = budget.reserve("gemini-test", 100_000, 240_000, research=True)
    dispatches += 1
    assert budget.settle(first, {
        "input_tokens": 100_000,
        "output_tokens": 200_000,
        "usd": 1.647855,
    })

    with pytest.raises(BudgetRefused):
        budget.reserve("gemini-test", 100_000, 240_000, research=True)

    assert dispatches == 1
    assert budget.booked_usd == 1.647855


def test_concurrent_reservations_cannot_overbook_the_question_hold():
    budget = make_budget(hold=0.003, research=0.003)
    barrier = Barrier(2)

    def try_reserve():
        barrier.wait()
        try:
            return reserve(budget)
        except BudgetRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: try_reserve(), range(2)))

    assert sum(ticket is not None for ticket in results) == 1
    assert budget.booked_usd == 0.002


def test_guarded_default_gemini_model_has_no_automatic_sdk_retry(monkeypatch):
    configure_gemini(monkeypatch)
    monkeypatch.setattr(sql_query, "BigQueryWarehouse", lambda: object())
    monkeypatch.setattr(warehouse, "BigQueryTableWriter", lambda: object())

    deps = ask._default_deps(gemini_retries=0)

    assert deps.model.retries == 0


def test_structured_gemini_partial_raw_usage_books_full_reservation(monkeypatch):
    configure_gemini(monkeypatch)
    response = SimpleNamespace(
        text="{}",
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=400,
            response_token_count=None,
            candidates_token_count=None,
            thoughts_token_count=10,
            tool_use_prompt_token_count=0,
            cached_content_token_count=0,
            total_token_count=None,
        ),
    )

    class Models:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return response

    client = SimpleNamespace(models=Models())
    monkeypatch.setattr(sql_query, "BigQueryWarehouse", lambda: object())
    monkeypatch.setattr(warehouse, "BigQueryTableWriter", lambda: object())
    from core.llm import provider as llm_provider

    monkeypatch.setattr(llm_provider, "make_model", lambda **kwargs: GeminiModel(client=client, retries=1))
    model = ask._default_deps(gemini_retries=0).model
    assert model.retries == 0
    budget = AskModelBudget(
        0.1,
        0.05,
        price_for_fn=lambda name: {"input": 1.5, "output": 7.5},
        reserve_output_fn=lambda name, tokens: tokens + 2000,
    )
    guarded = ask._StopAwareModel(model, lambda: False, budget)

    # The call succeeded, so its unverifiable usage books the full reserve and the ask goes on (it used to stop).
    out, _ = guarded.complete_json(system="s", user="u", schema={"type": "object"},
                                   model="gemini-test", max_tokens=10)

    assert out == {}
    assert len(client.models.calls) == 1
    assert budget.booked_usd == 0.0152
    assert budget.conservative_usd == 0.0152
    assert not budget.stopped
    guarded.complete_json(system="s", user="u", schema={"type": "object"}, model="gemini-test", max_tokens=10)
    assert len(client.models.calls) == 2
    assert budget.booked_usd == budget.conservative_usd == 0.0304


def test_succeeded_call_with_unknown_usage_books_the_reserve_and_leaves_room_for_the_next():
    budget = make_budget(hold=0.004, research=0.004)
    ticket = reserve(budget, research=False)

    assert budget.settle(ticket, {"input_tokens": 100, "usd": 0.0001}, stop_unknown=False)

    assert budget.booked_usd == budget.conservative_usd == 0.002
    assert not budget.stopped and budget.stop_reason is None
    second = reserve(budget, research=False)
    assert budget.settle(second, {"input_tokens": 100, "output_tokens": 100, "usd": 0.0002})
    with pytest.raises(BudgetRefused, match="question_model_budget_exhausted"):
        reserve(budget, research=False)  # the reserves booked in full still count against the cap


class _Incomplete:
    """A model whose calls succeed or fail as scripted, with a usage report the budget cannot verify."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def usage_is_complete(self):
        return False


def _guarded_call(model):
    return model.complete_json(system="s", user="u", schema={}, model="gemini-test", max_tokens=10)


def test_guarded_success_with_incomplete_usage_continues_but_a_failed_call_still_stops():
    budget = make_budget(hold=0.01, research=0.01)
    usage = {"input_tokens": 10, "output_tokens": 5, "usd": 0.000015}
    inner = _Incomplete(({"ok": 1}, usage), ({"ok": 2}, usage), RuntimeError("returned no text"), ({"ok": 3}, usage))
    model = ask._StopAwareModel(inner, lambda: False, budget)

    assert _guarded_call(model)[0] == {"ok": 1}
    assert _guarded_call(model)[0] == {"ok": 2}
    assert not budget.stopped
    with pytest.raises(RuntimeError, match="returned no text"):
        _guarded_call(model)
    assert budget.stopped and budget.stop_reason == "usage_unknown"
    with pytest.raises(ask._StopRequested) as stopped:
        _guarded_call(model)
    assert stopped.value.before_dispatch and inner.calls == 3
    assert budget.conservative_usd == budget.booked_usd > 0


def test_guarded_success_with_usage_outside_its_reserve_still_stops():
    budget = make_budget(hold=0.01, research=0.01)
    over = {"input_tokens": 10_000, "output_tokens": 5, "usd": 0.01}

    class Over:
        def complete_json(self, **kwargs):
            return {"ok": True}, over

    model = ask._StopAwareModel(Over(), lambda: False, budget)
    with pytest.raises(ask._StopRequested) as stopped:
        _guarded_call(model)

    assert stopped.value.model_budget_reason == "model_usage_unknown_or_outside_reserve"
    assert budget.stopped and budget.stop_reason == "bound_exceeded"


class _IncompleteChecksModel(RecordingModel):
    """The writer reports usage in full; every later call (support, K4, field check) reports it incompletely."""

    def __init__(self):
        super().__init__()
        self.last_was_writer = False

    def complete_json(self, **kwargs):
        self.last_was_writer = kwargs["schema"] is WRITER_SCHEMA
        return super().complete_json(**kwargs)

    def usage_is_complete(self):
        return self.last_was_writer


def test_run_ask_finishes_when_checks_after_the_writer_report_incomplete_usage(monkeypatch):
    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)
    model = _IncompleteChecksModel()

    result = Harness(model=model).run(tier="T1")

    budget, = _RecordingBudget.made
    assert len(model.calls) > 1  # the writer and at least one check after it
    assert not budget.stopped
    assert result["answer"]["claims"]
    assert not any(gap["why"] == ask.BUDGET_STOP for gap in result["answer"]["gaps"])
    assert any("the most that call could have cost" in notice for notice in result["run"]["notices"])
    assert budget.conservative_usd > 0
    assert result["run"]["model_usd"] >= budget.booked_usd


@pytest.mark.parametrize("tier", ["T0", "T1"])
def test_run_ask_shares_the_meter_across_research_and_writer_once(monkeypatch, tier):
    configure_gemini(monkeypatch)
    research = make_research()
    seen = {}
    research_usd = 0.000525
    research_usage = {"input_tokens": 100, "output_tokens": 50, "usd": research_usd}

    def bounded_research(ctx, prompt, options, emit, should_stop):
        seen["budget"], seen["ctx"] = ctx.model_budget, ctx
        reservation = ctx.model_budget.reserve(options.model, 1000, 8000, research=True)
        assert ctx.model_budget.settle(reservation, research_usage)
        result = research(ctx, prompt, options, emit, should_stop)
        result["usd"] = research_usd
        return result

    model = RecordingModel()
    harness = Harness(research=bounded_research, model=model)
    result = harness.run(tier=tier)

    assert model.calls
    assert result["run"]["model_usd"] == pytest.approx(
        seen["budget"].booked_usd)
    assert result["run"]["model_usd"] == pytest.approx(
        research_usd + sum(usage["usd"] for usage in model.usages) + seen["ctx"].model_usd_extra)
    assert result["run"]["notices"] == [CONSERVATIVE_NOTICE]
    assert seen["budget"].conservative_usd == SEARCH_EMBED_USD


def test_research_subbudget_stop_keeps_writer_available(monkeypatch):
    configure_gemini(monkeypatch)
    research = make_research()
    seen = {}
    research_usage = {"input_tokens": 100_000, "output_tokens": 200_000, "usd": 1.65}

    def bounded_research(ctx, prompt, options, emit, should_stop):
        budget = ctx.model_budget
        seen["budget"], seen["ctx"] = budget, ctx
        first = budget.reserve(options.model, 100_000, 240_000, research=True)
        assert budget.settle(first, research_usage)
        result = research(ctx, prompt, options, emit, should_stop)
        with pytest.raises(BudgetRefused):
            budget.reserve(options.model, 100_000, 240_000, research=True)
        result["usd"] = research_usage["usd"]
        return result

    model = RecordingModel()
    harness = Harness(research=bounded_research, model=model)
    result = harness.run(tier="T1")

    assert model.calls
    assert seen["budget"].research_exhausted
    assert not seen["budget"].stopped
    assert result["run"]["model_usd"] == pytest.approx(
        seen["budget"].booked_usd)
    assert any("stopped at its share of the model budget" in notice for notice in result["run"]["notices"])


def test_budget_stop_does_not_claim_the_user_requested_it(monkeypatch):
    configure_gemini(monkeypatch)

    def unknown_research(ctx, prompt, options, emit, should_stop):
        ticket = ctx.model_budget.reserve(options.model, 1000, 8000, research=True)
        cost = ctx.model_budget.fail(ticket)
        return {"note": "", "tokens": {"input": 0, "output": 0}, "usd": cost, "stopped": True}

    harness = Harness(research=unknown_research)
    result = harness.run(tier="T1")

    assert result["answer"]["short_answer"].startswith("The answer was not finished")  # reworded 6 Oct: plain words
    assert "Stopped on request" not in result["answer"]["short_answer"]
    assert "the most that call could have cost" in " ".join(result["run"]["notices"])
    assert harness.model.calls == []


@pytest.mark.parametrize("usage_kind", ["missing", "partial"])
def test_run_ask_books_full_gemini_research_reserve_into_daily_projection(monkeypatch, usage_kind):
    configure_gemini(monkeypatch)
    response_usage = None if usage_kind == "missing" else SimpleNamespace(
        prompt_token_count=400,
        response_token_count=None,
        candidates_token_count=None,
        thoughts_token_count=10,
        tool_use_prompt_token_count=0,
        cached_content_token_count=0,
    )
    response = SimpleNamespace(
        usage_metadata=response_usage,
        candidates=[SimpleNamespace(
            finish_reason=SimpleNamespace(name="STOP"),
            content=SimpleNamespace(parts=[]),
        )],
    )

    class Models:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return response

    client = SimpleNamespace(models=Models())
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    seen = {}

    def research(ctx, prompt, options, emit, should_stop):
        seen["budget"], seen["ctx"] = ctx.model_budget, ctx
        return gemini_research.gemini_research(ctx, prompt, options, emit, should_stop)

    first = Harness(research=research)
    first_result = first.run(tier="T1")
    booked = first_result["run"]["model_usd"]

    assert client.models.calls and len(client.models.calls) == 1
    assert first.model.calls == []
    assert booked == pytest.approx(seen["budget"].booked_usd)
    assert booked > 0
    assert any("the most that call could have cost" in notice for notice in first_result["run"]["notices"])

    projected_sql = []

    class DailyProjection:
        def run(self, sql, params, max_bytes_billed):
            projected_sql.append(sql)
            return [{"usd": booked}]

    daily_cap = ask.hold_usd("T0", "gemini-3.8-flash") + booked / 2
    monkeypatch.setattr(ask, "model_daily_usd", lambda now: daily_cap)
    second = Harness(research=research)
    second.deps.spent_today_usd = lambda: ask.model_spend_today(
        DailyProjection(), datetime(2026, 9, 28, 8, 15, tzinfo=timezone(timedelta(hours=2))))
    second_result = second.run(tier="T1")

    assert projected_sql == [ask.SPEND_SQL]
    assert second.model.calls == []
    assert second_result["run"]["model_usd"] == 0


def _flag_every_budget_notice(budget, options):
    """Book a full reserve, then set the flags behind every model budget notice run_ask can show."""
    budget.fail(budget.reserve(options.model, 1000, 8000, research=True))
    budget._research_exhausted = True
    budget._bound_exceeded = True
    budget._ceiling_exceeded = True


def _shown_text(answer, run):
    """Every string of an ask the app puts on screen: the notices, the short answer and each gap's what and why."""
    return [*run["notices"], answer["short_answer"] if answer else "",
            *[g[key] for g in (answer or {}).get("gaps", []) for key in ("what", "why")]]


@pytest.mark.parametrize("path", ["research_stopped", "writer_refused", "research_failed"])
def test_no_model_budget_notice_or_short_answer_names_the_vendor_or_an_internal_field(monkeypatch, path):
    configure_gemini(monkeypatch)

    def research(ctx, prompt, options, emit, should_stop):
        _flag_every_budget_notice(ctx.model_budget, options)
        if path == "research_failed":
            raise RuntimeError("research broke before it reported usage")
        ctx.model_budget._stopped = True
        return {"note": "", "tokens": {"input": 0, "output": 0}, "usd": 0.0, "stopped": path == "research_stopped"}

    harness = Harness(research=research)
    if path == "research_failed":
        with pytest.raises(RuntimeError) as failed:
            harness.run(tier="T1")
        answer, run = None, failed.value.run
    else:
        result = harness.run(tier="T1")
        answer, run = result["answer"], result["run"]

    shown = _shown_text(answer, run)
    assert len(run["notices"]) >= 5  # the four budget flags and the stop or failure notice
    leaks = [text for text in shown if "gemini" in text.lower() or "model_usd" in text]
    assert leaks == []


def test_the_tier_fallback_notice_never_names_a_vendor_model_id(monkeypatch):
    configure_gemini(monkeypatch)
    monkeypatch.setattr(ask, "MODEL", "gemini-3.8-pro")
    cap = ask.model_daily_usd(now=NOW)
    harness = Harness(spent=lambda: cap - ask.hold_usd("T1", "gemini-3.8-pro") + 0.01)

    run = harness.run(tier="T1")["run"]

    assert run["tier"] == "T0"
    fallback = [n for n in run["notices"] if f"USD {cap:g}" in n]
    assert fallback and not any("gemini" in n.lower() for n in run["notices"])


class _RecordingBudget(AskModelBudget):
    """The real budget, keeping itself and its reservations for the test to read."""
    made = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reservations = []
        _RecordingBudget.made.append(self)

    def reserve(self, *args, **kwargs):
        reservation = super().reserve(*args, **kwargs)
        self.reservations.append(reservation)
        return reservation


class _TimedOutModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, **kwargs):
        import httpx

        self.calls.append(kwargs)
        raise httpx.ReadTimeout("timed out")


def test_a_writer_timeout_fails_the_ask_like_any_provider_error_and_books_the_full_reserve(monkeypatch):
    import httpx

    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)
    models = _TimedOutModels()
    harness = Harness(model=GeminiModel(client=SimpleNamespace(models=models), retries=0))

    with pytest.raises(httpx.ReadTimeout) as caught:
        harness.run(tier="T1")

    budget, = _RecordingBudget.made
    writer, = budget.reservations
    assert len(models.calls) == 1
    assert budget.conservative_usd == pytest.approx(writer.ceiling_micros / 1_000_000 + SEARCH_EMBED_USD)
    assert budget.booked_usd == budget.conservative_usd
    assert budget.stop_reason == "usage_unknown"
    assert caught.value.run["model_usd"] >= budget.booked_usd


def test_a_research_timeout_fails_the_ask_like_any_provider_error_and_books_the_full_reserve(monkeypatch):
    import httpx

    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)
    models = _TimedOutModels()
    monkeypatch.setattr(gemini_research, "client_factory", lambda: SimpleNamespace(models=models))
    harness = Harness(research=gemini_research.gemini_research)

    with pytest.raises(httpx.ReadTimeout) as caught:
        harness.run(tier="T1")

    budget, = _RecordingBudget.made
    research, = budget.reservations
    assert len(models.calls) == 1 and harness.model.calls == []
    assert research.research
    assert budget.booked_usd == budget.conservative_usd == research.ceiling_micros / 1_000_000
    assert caught.value.before_dispatch is False
    assert caught.value.run["model_usd"] >= budget.booked_usd
    assert any("the most it could have cost" in notice for notice in caught.value.run["notices"])
