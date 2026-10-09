"""A 429 for capacity bills nothing: Ask gives its reserve back and waits it out instead of stopping the question.

Live 6 Oct 2026: Vertex refused calls with 429 RESOURCE_EXHAUSTED on and off. Each guarded Ask call booked its full
reserve on the first one and stopped every later call, so a ZA ask that had used USD 0.43 of its 4.27 hold ended
"could not be safely reserved" at the checks.
"""

from types import SimpleNamespace

import pytest
from google.genai import errors

from core.agent import ask, gemini_research
from core.agent.model_budget import AskModelBudget, BudgetRefused
from core.agent.tests.test_ask import SEARCH_EMBED_USD, FakeModel, Harness
from core.agent.tests.test_ask_model_budget import _RecordingBudget, configure_gemini
from core.agent.tests.test_gemini_research_budget import FakeClient, ftext, make_budget, reply, run
from core.agent.writer import SUPPORT_SCHEMA, WRITER_SCHEMA


def busy():
    return errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted. Please try again later.",
                                              "status": "RESOURCE_EXHAUSTED"}})


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    slept = []
    monkeypatch.setattr(gemini_research.time, "sleep", slept.append)
    return slept


def plain_budget(hold=0.01, research=0.01):
    return AskModelBudget(hold, research, price_for_fn=lambda model: {"input": 1.0, "output": 1.0},
                          reserve_output_fn=lambda model, tokens: tokens)


def test_release_gives_back_exactly_the_reserve_and_books_nothing():
    budget = plain_budget(hold=0.003, research=0.002)
    first = budget.reserve("gemini-test", 1000, 1000, research=True)
    with pytest.raises(BudgetRefused, match="research_model_budget_exhausted"):
        budget.reserve("gemini-test", 1, 1, research=True)  # the research share is held in full by the first

    budget.release(first)

    assert budget.booked_usd == 0 and budget.conservative_usd == 0
    assert not budget.stopped
    again = budget.reserve("gemini-test", 1000, 1000, research=False)  # the whole hold is back
    budget.reserve("gemini-test", 500, 500, research=False)
    with pytest.raises(BudgetRefused, match="question_model_budget_exhausted"):
        budget.reserve("gemini-test", 1, 1, research=False)
    assert budget.booked_usd == pytest.approx(0.003)
    assert again.ceiling_micros == 2000


def test_release_of_an_unknown_or_settled_reservation_stops_later_calls():
    budget = plain_budget()
    ticket = budget.reserve("gemini-test", 1000, 1000)
    budget.settle(ticket, {"input_tokens": 10, "output_tokens": 5, "usd": 0.000015})

    with pytest.raises(BudgetRefused, match="model_reservation_unknown"):
        budget.release(ticket)
    assert budget.stopped and budget.stop_reason == "model_reservation_unknown"
    assert budget.booked_usd == pytest.approx(0.000015)


def test_only_a_coded_429_with_no_usage_counts_as_a_refusal():
    assert gemini_research.busy_refusal(busy())
    billed = busy()
    billed.usage = {"input_tokens": 10, "output_tokens": 5, "usd": 0.1}
    assert not gemini_research.busy_refusal(billed)
    assert not gemini_research.busy_refusal(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert not gemini_research.busy_refusal(errors.ServerError(503, {"error": {"code": 503}}))


def test_research_waits_out_a_429_on_a_fresh_reserve_and_books_only_the_call_that_ran(monkeypatch, no_waiting):
    budget, _ = make_budget()
    client = FakeClient(busy(), busy(), reply(ftext("Reading.")))

    result, ctx = run(monkeypatch, client, budget)

    assert result["note"] == "Reading." and not result["stopped"]
    assert len(client.calls) == 3
    assert not budget.stopped and budget.conservative_usd == 0
    assert budget.booked_usd == pytest.approx(result["usd"], abs=1e-6)  # the two refusals booked nothing
    assert sum(no_waiting) == pytest.approx(5 + 10)


def test_research_gives_up_after_the_bounded_waits_and_books_nothing(monkeypatch, no_waiting):
    budget, _ = make_budget()
    client = FakeClient(busy(), busy(), busy(), busy())

    with pytest.raises(errors.ClientError):
        run(monkeypatch, client, budget)

    assert len(client.calls) == 1 + len(gemini_research.BUSY_WAITS_S)
    assert sum(no_waiting) == pytest.approx(sum(gemini_research.BUSY_WAITS_S)) == 35
    assert budget.booked_usd == 0 and not budget.stopped


def test_a_stop_during_the_wait_ends_it_without_another_call(monkeypatch):
    budget, _ = make_budget()
    client = FakeClient(busy(), reply(ftext("never")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)
    from core.agent.tests.test_gemini_research_budget import setup
    ctx, options, emit = setup(budget)
    stops = iter([False, False, True])

    with pytest.raises(errors.ClientError):
        gemini_research.gemini_research(ctx, "Question?", options, emit, lambda: next(stops, True))

    assert len(client.calls) == 1 and budget.booked_usd == 0


def test_research_retry_never_goes_out_once_another_call_stopped_the_question(monkeypatch):
    budget, _ = make_budget()
    calls = []

    def generate_content(**kwargs):
        calls.append(kwargs)
        other = budget.reserve("gemini-test", 1000, 1)  # another call fails with unknown usage meanwhile
        budget.settle(other, None)
        raise busy()

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))

    with pytest.raises(errors.ClientError):
        run(monkeypatch, client, budget)

    assert len(calls) == 1  # the retry could not reserve, so the refusal stands
    assert budget.booked_usd == budget.conservative_usd  # only the other call's reserve is booked


class _BusyThenOk:
    def __init__(self, refusals, usage=None):
        self.refusals, self.calls = refusals, 0
        self.usage = usage or {"input_tokens": 10, "output_tokens": 5, "usd": 0.000015}

    def complete_json(self, **kwargs):
        self.calls += 1
        if self.calls <= self.refusals:
            raise busy()
        return {"ok": True}, self.usage


def _call(model):
    return model.complete_json(system="s", user="u", schema={}, model="gemini-test", max_tokens=10)


def test_a_guarded_check_call_waits_out_a_429_and_the_question_carries_on(no_waiting):
    budget = plain_budget()
    inner = _BusyThenOk(refusals=2)
    model = ask._StopAwareModel(inner, lambda: False, budget)

    assert _call(model)[0] == {"ok": True}

    assert inner.calls == 3 and sum(no_waiting) == pytest.approx(15)
    assert budget.booked_usd == pytest.approx(inner.usage["usd"])
    assert not budget.stopped and budget.conservative_usd == 0


def test_a_guarded_call_that_stays_refused_raises_billed_at_nothing_and_stops_nothing():
    budget = plain_budget()
    inner = _BusyThenOk(refusals=10)
    model = ask._StopAwareModel(inner, lambda: False, budget)

    with pytest.raises(errors.ClientError) as raised:
        _call(model)

    assert inner.calls == 1 + len(gemini_research.BUSY_WAITS_S)
    assert raised.value.usage == {"input_tokens": 0, "output_tokens": 0, "usd": 0.0}
    assert budget.booked_usd == 0 and not budget.stopped
    inner.refusals = 0
    assert _call(model)[0] == {"ok": True}  # the next call still runs within the cap


def test_a_429_that_reports_spend_is_booked_not_released():
    budget = plain_budget()

    class Billed:
        def complete_json(self, **kwargs):
            exc = busy()
            exc.usage = {"input_tokens": 10, "output_tokens": 5, "usd": 0.000015}
            raise exc

    model = ask._StopAwareModel(Billed(), lambda: False, budget)
    with pytest.raises(errors.ClientError):
        _call(model)

    assert budget.booked_usd == pytest.approx(0.000015)


class _SupportBusyOnce(FakeModel):
    """The first support check is refused for capacity once, as on 6 Oct; every other call goes through."""

    def __init__(self):
        super().__init__()
        self.refused = 0

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is SUPPORT_SCHEMA and not self.refused:
            self.refused += 1
            raise busy()
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def test_run_ask_finishes_when_a_support_check_is_refused_for_capacity(monkeypatch):
    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)
    model = _SupportBusyOnce()

    result = Harness(model=model).run(tier="T1")

    budget, = _RecordingBudget.made
    assert model.refused == 1
    assert not budget.stopped and budget.conservative_usd == SEARCH_EMBED_USD
    assert result["answer"]["claims"]
    assert not any("budget" in gap["why"] for gap in result["answer"]["gaps"])
    assert not any("stopped" in notice.lower() for notice in result["run"]["notices"])
    assert result["run"]["model_usd"] >= budget.booked_usd
    assert len(model.calls) > 1 and model.calls[0]["schema"] is WRITER_SCHEMA


def test_watch_video_spending_model_waits_out_a_429_and_books_only_the_read(no_waiting):
    from core.agent.context import RunContext
    from core.agent.tools import enrich_tools

    budget = plain_budget()
    ctx = RunContext(run_id="r_video", tier="T1", as_of=None, market="ZA")
    ctx.model_budget = budget
    inner = _BusyThenOk(refusals=1)
    model = enrich_tools._SpendingModel(ctx, inner, 1000)

    assert _call(model)[0] == {"ok": True}

    assert inner.calls == 2 and sum(no_waiting) == pytest.approx(5)
    assert budget.booked_usd == pytest.approx(inner.usage["usd"]) and not budget.stopped


def test_watch_video_under_a_budget_builds_its_client_without_its_own_retry(monkeypatch):
    from core.llm import gemini

    made = []

    class Stub:
        def __init__(self, *args, retries=1, **kwargs):
            made.append(retries)

    monkeypatch.setattr(gemini, "GeminiModel", Stub)
    from core.agent.context import RunContext
    from core.agent.tools import enrich_tools

    ctx = RunContext(run_id="r_video", tier="T1", as_of=None, market="ZA")
    ctx.model_budget = plain_budget()
    def read_clip(*args, **kwargs):
        raise RuntimeError("stop")

    monkeypatch.setattr("core.understand.video.read_clip", read_clip)
    ctx.evidence["tt_1"] = {"platform": "tiktok", "url": "https://www.tiktok.com/@a/video/1", "text": "x"}
    monkeypatch.setattr(enrich_tools, "_creator_cleared", lambda *a, **k: None)
    monkeypatch.setattr(enrich_tools, "_stored_video", lambda *a, **k: {})
    monkeypatch.setattr("core.llm.provider.provider", lambda: "gemini")
    monkeypatch.setattr("core.config.caps.video_daily", lambda: {"clips": 5, "credits": 5})

    with pytest.raises(RuntimeError, match="stop"):
        enrich_tools.watch_video(ctx, None, "tt_1", "What happens?", warehouse=None)

    assert made == [0]


def test_a_budget_stop_after_a_call_with_no_usage_report_says_so_not_that_money_ran_out(monkeypatch):
    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)
    client = FakeClient(reply(ftext("Reading."), metadata=None))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)

    result = Harness(research=gemini_research.gemini_research).run(tier="T1")

    budget, = _RecordingBudget.made
    assert budget.stop_reason == "usage_unknown"
    assert budget.booked_usd < budget.cap_micros / 1_000_000 / 10  # far inside the hold: not a money stop
    gap = next(g for g in result["answer"]["gaps"] if g["why"] == ask.BUDGET_STOP)
    assert gap["what"] == "The answer stopped because a model call failed or did not report what it cost"
    assert result["answer"]["short_answer"].endswith("You can ask again.")
    assert any(n.startswith("A model call failed or did not report what it cost") for n in result["run"]["notices"])
    assert not any("verified budget" in n or "per-call reserve" in n for n in result["run"]["notices"])
    assert result["run"]["followups"] == ["Can you run this question again to the end?"]


class _LaterSupportFails(FakeModel):
    """The support check for c3 fails with no usage report while the others pass; c2's K4 rewrite comes after it in
    claim order. This is the 6 Oct shape: a failed check stopped the budget and the next call was refused."""

    def complete_json(self, *, system, user, schema, model, max_tokens):
        if schema is SUPPORT_SCHEMA and user.startswith("Claim: A Durban"):
            raise RuntimeError("returned no text")
        return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)


def test_a_failed_check_then_a_refused_rewrite_reads_as_a_failed_call(monkeypatch):
    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)

    result = Harness(model=_LaterSupportFails()).run(tier="T1")

    budget, = _RecordingBudget.made
    assert budget.stop_reason == "usage_unknown"
    assert result["answer"]["gaps"][-1]["what"] == ask.budget_stop_words("usage_unknown")[0]
    assert ask.budget_stop_words("usage_unknown")[2] in result["run"]["notices"]


@pytest.mark.parametrize("reason, start", [
    ("question_model_budget_exhausted", "The answer stopped because the next model call would not fit"),
    ("bound_exceeded", "The answer stopped because the next model call would not fit"),
    ("model_price_unavailable", "The answer stopped because the cost of a model call could not be worked out"),
    ("usage_unknown", "The answer stopped because a model call failed"),
    (None, "The answer stopped because the next model call would not fit"),
])
def test_each_budget_stop_reason_reads_in_plain_words(reason, start):
    what, short, notice = ask.budget_stop_words(reason)
    assert what.startswith(start)
    for text in (what, short, notice):
        assert "reserve" not in text and "verified" not in text and "—" not in text


def test_a_tool_that_stops_the_budget_between_research_turns_gives_a_budget_stop_answer_not_a_failed_ask(monkeypatch):
    # A5: watch_video's model failure with no usage report stops the shared budget; the next research turn was refused
    # with usage_unknown and the refusal escaped, so the ask failed instead of answering with the plain budget stop.
    configure_gemini(monkeypatch)
    _RecordingBudget.made = []
    monkeypatch.setattr(ask, "AskModelBudget", _RecordingBudget)

    def build(ctx, warehouse, client, tables):
        def stop_budget():
            budget = ctx.model_budget
            budget.settle(budget.reserve("gemini-3.8-flash", 1000, 100, research=True), None)
            return {"ok": True}
        return {"budget_status": stop_budget}

    monkeypatch.setattr(gemini_research, "build_functions", build)
    from core.agent.tests.test_gemini_research_budget import fcall
    client = FakeClient(reply(fcall("budget_status")), reply(ftext("never")))
    monkeypatch.setattr(gemini_research, "client_factory", lambda: client)

    result = Harness(research=gemini_research.gemini_research).run(tier="T1")

    budget, = _RecordingBudget.made
    assert budget.stop_reason == "usage_unknown" and len(client.calls) == 1
    gap = next(g for g in result["answer"]["gaps"] if g["why"] == ask.BUDGET_STOP)
    assert gap["what"] == ask.budget_stop_words("usage_unknown")[0]
    assert result["answer"]["status"] == "insufficient_evidence"
    assert budget.booked_usd < budget.cap_micros / 1_000_000  # inside the hold
