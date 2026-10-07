"""Brief dispatch deadlines through real Gemini configuration, without network calls."""

import json
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from google import genai

from core.brief import job
from core.brief.tests.test_brief_busy import Clock
from core.brief.tests.test_brief_gemini_path import FakeModels
from core.brief.tests.test_brief_deadline_transport import DelayedCredentials
from core.brief.tests.test_brief_job import (
    D, MARKETS, SAST, Client, FakeChain, FakeConfirm, FakeCtx, all_cards, busy_waits, checks,
    market_scope_is_valid_by_default, payload, world,
)
from core.llm.gemini import GeminiModel

DEADLINE = datetime.combine(D, time(6, 15), SAST)


class TimedModels(FakeModels):
    def __init__(self, clock, *, exhaust_after=None, stall=False, rewrite=False, title=False, refuse_first=False):
        super().__init__()
        self.clock, self.exhaust_after = clock, exhaust_after
        self.stall, self.rewrite, self.title = stall, rewrite, title
        self.refuse_first = refuse_first
        self.dispatches, self.writers = [], 0

    def handle(self, request):
        body = json.loads(request.content)
        contents = "\n".join(part.get("text", "") for content in body.get("contents", [])
                              for part in content.get("parts", []))
        config = self.config
        properties = config.response_json_schema["properties"]
        phase = "critic" if "ruled_out" in properties else "support" if "verdict" in properties else "writer"
        if phase == "writer":
            self.writers += 1
            phase = "rewrite" if self.writers > 1 else phase
        options = config.http_options
        self.dispatches.append({"at": self.clock.now, "phase": phase,
                                "timeout_ms": request.extensions["timeout"]["read"] * 1000,
                                "configured_timeout_ms": options.timeout if options else None,
                                "attempts": options.retry_options.attempts if options and options.retry_options else None})
        if self.refuse_first and len(self.dispatches) == 1:
            return httpx.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                       "message": "scripted capacity refusal"}}, request=request)
        if self.stall:
            self.clock.now += timedelta(seconds=request.extensions["timeout"]["read"])
            raise httpx.ReadTimeout("scripted stall")
        response = super().generate_content(model="gemini-3.8-flash", contents=contents, config=config)
        output = json.loads(response.text)
        if phase == "critic" and self.rewrite:
            output["local_why_now"] = False
        if phase in ("writer", "rewrite") and self.title:
            output["title"] = "Dance clips shared at home"
        if len(self.dispatches) == self.exhaust_after:
            self.clock.now = DEADLINE
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(output)}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 120, "cachedContentTokenCount": 20,
                              "candidatesTokenCount": 30, "thoughtsTokenCount": 40},
        }, request=request)


@contextmanager
def native_model(clock, sdk, timeout_s=60):
    original = genai.Client
    original_async = httpx.AsyncClient
    clients = []

    def make(**kwargs):
        options = kwargs["http_options"]
        args = {**(options.client_args or {}), "transport": httpx.MockTransport(sdk.handle)}
        kwargs["http_options"] = options.model_copy(update={"client_args": args})
        kwargs["credentials"] = DelayedCredentials(clock, 0)
        client = original(**kwargs)
        clients.append(client)
        return client

    model = GeminiModel(timeout_s=timeout_s)
    original_config = model.config

    def record_config(**kwargs):
        sdk.config = original_config(**kwargs)
        return sdk.config

    model.config = record_config

    def make_async(**kwargs):
        return original_async(**{**kwargs, "transport": httpx.MockTransport(sdk.handle)})

    try:
        with patch("google.genai.Client", make), patch("httpx.AsyncClient", make_async):
            yield model
    finally:
        model.close_deadline_calls()
        for client in clients:
            client.close()


def brief(clock, sdk, *, n=1, timeout_s=60):
    client, chain = Client(world(n=n, markets=("ZA",))), FakeChain()
    with native_model(clock, sdk, timeout_s) as model:
        counts = job.run(client, D, chain=chain, model=model, make_sc=lambda run_id: object(), clock=clock,
                         build_ctx=FakeCtx(), confirm=FakeConfirm(), campaign_hashtags=[],
                         political_terms={m: [] for m in MARKETS}, workers=1, core="core", agent="agent",
                         sleep=clock.sleep)
    return SimpleNamespace(client=client, chain=chain, counts=counts)


@pytest.fixture(autouse=True)
def deadline_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    for key in ("FORCE_RERUN", "RUN_DATE", "BRIEF_RECOVERY_UNTIL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("exhaust_after", [1, 2, 5, 6, 7], ids=["writer", "support", "before_critic", "before_rewrite", "rewrite"])
def test_no_phase_dispatches_after_the_deadline(exhaust_after):
    clock = Clock(DEADLINE - timedelta(seconds=10))
    sdk = TimedModels(clock, exhaust_after=exhaust_after, rewrite=True)
    result = brief(clock, sdk)
    assert len(sdk.dispatches) == exhaust_after
    assert all(call["at"] < DEADLINE and call["timeout_ms"] <= 10000 and call["attempts"] == 1
               for call in sdk.dispatches)
    assert payload(result, "ZA")["cards"] == []
    assert payload(result, "ZA")["held_back"]["count"] == 1
    assert checks(result)
    assert result.chain.finished[0]["status"] == "ok"


def test_a_title_support_check_is_not_paid_after_the_critic_reaches_the_deadline():
    clock = Clock(DEADLINE - timedelta(seconds=10))
    sdk = TimedModels(clock, exhaust_after=6, title=True)
    result = brief(clock, sdk)
    assert len(sdk.dispatches) == 6
    [card] = all_cards(payload(result, "ZA"))
    assert card["title_written"] is None
    assert any(row["rule"] == "title" and row["verdict"] == "cut" for row in checks(result))


def test_two_scripted_read_timeouts_recompute_the_next_dispatch_allowance():
    clock = Clock(DEADLINE - timedelta(seconds=100))
    sdk = TimedModels(clock, stall=True)
    result = brief(clock, sdk, n=3)
    assert len(sdk.dispatches) >= 2
    assert all(0 < call["timeout_ms"] <= min(60000, (DEADLINE - call["at"]).total_seconds() * 1000)
               for call in sdk.dispatches)
    assert all(call["attempts"] == 1 and call["at"] < DEADLINE for call in sdk.dispatches)
    assert clock.now <= DEADLINE
    assert payload(result, "ZA")["held_back"]["count"] == 3
    assert result.chain.finished[0]["status"] == "ok"


def test_a_capacity_retry_rechecks_the_deadline_after_sleep_returns(monkeypatch):
    monkeypatch.setenv("BRIEF_BUSY_JITTER", "0")
    clock = Clock(DEADLINE - timedelta(seconds=25))
    sdk = TimedModels(clock, refuse_first=True)

    def delayed_wakeup(seconds):
        clock.slept.append(seconds)
        clock.now = DEADLINE

    clock.sleep = delayed_wakeup
    result = brief(clock, sdk)
    assert len(sdk.dispatches) == 1 and clock.slept == [10]
    assert result.counts["model_usd"] == 0
    assert payload(result, "ZA")["held_back"]["count"] == 1
    assert result.chain.finished[0]["status"] == "ok"


@pytest.mark.parametrize("remaining,timeout_s,expected", [(20, 60, 20000), (90, 60, 60000), (90, 15, 15000)])
def test_request_timeout_is_the_smaller_existing_limit_or_remaining_time(remaining, timeout_s, expected):
    clock = Clock(DEADLINE - timedelta(seconds=remaining))
    sdk = TimedModels(clock, stall=True)
    brief(clock, sdk, timeout_s=timeout_s)
    assert sdk.dispatches[0]["configured_timeout_ms"] == expected
    assert 0 < sdk.dispatches[0]["timeout_ms"] <= expected
    assert sdk.dispatches[0]["attempts"] == 1


def test_recovery_deadline_replaces_the_normal_daily_deadline(monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "07:00")
    until = datetime.combine(D, time(7, 0), SAST)
    clock = Clock(until - timedelta(seconds=9))
    sdk = TimedModels(clock, stall=True)
    brief(clock, sdk)
    assert len(sdk.dispatches) == 1 and sdk.dispatches[0]["configured_timeout_ms"] == 9000
    assert 0 < sdk.dispatches[0]["timeout_ms"] <= 9000
    assert clock.now <= until


def test_task_finish_cutoff_keeps_the_eight_minute_margin_before_the_hour():
    start = datetime.combine(D, time(4, 0), SAST)
    cutoff = start + job.collect_chain.TIMEOUTS["brief"] - job.FINISH_MARGIN
    clock = Clock(cutoff - timedelta(seconds=11))
    sdk = TimedModels(clock, stall=True)
    tasks = job._in_rank_order(job._candidates(Client(world(n=2, markets=("ZA",))), D, build_ctx=FakeCtx(),
                                               campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                                               core="core", agent="agent"))
    with native_model(clock, sdk, job.GEMINI_TIMEOUT_S) as model:
        results, _, stop = job._explain_all(tasks, model=model, base_usd=0, spend={"usd": 0}, clock=clock,
                                           chain=FakeChain(), d=D, workers=1, started=start, sleep=clock.sleep)
    assert job.FINISH_MARGIN == timedelta(minutes=8) and job.GEMINI_TIMEOUT_S == 60
    assert sdk.dispatches and sdk.dispatches[0]["configured_timeout_ms"] == 11000
    assert 0 < sdk.dispatches[0]["timeout_ms"] <= 11000
    assert all(call["at"] < cutoff and call["attempts"] == 1
               and 0 < call["timeout_ms"] <= (cutoff - call["at"]).total_seconds() * 1000
               for call in sdk.dispatches)
    assert clock.now <= cutoff
    assert results and (stop is None or stop["limit"] == "task_timeout")
