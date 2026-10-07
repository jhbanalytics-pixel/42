"""Deadline admission through installed Vertex SDK authentication and HTTPX transport."""

from datetime import datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.auth.credentials import Credentials

from core.brief import job
from core.brief.tests.test_brief_job import D, SAST, FakeChain
from core.llm.gemini import GeminiModel

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


class DelayedCredentials(Credentials):
    def __init__(self, clock, seconds):
        super().__init__()
        self.clock, self.seconds = clock, seconds

    def refresh(self, request):
        self.clock.now += timedelta(seconds=self.seconds)
        self.token = "offline-test-value"
        self.expiry = datetime(2099, 1, 1)


def offline_factory(monkeypatch, clock, delay, calls):
    original = genai.Client
    original_async = httpx.AsyncClient
    clients = []

    def handle(request):
        calls.append({"at": clock.now, "timeout": dict(request.extensions.get("timeout", {}))})
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"answer":"ok"}'}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }, request=request)

    def make(**kwargs):
        options = kwargs["http_options"]
        args = {**(options.client_args or {}), "transport": httpx.MockTransport(handle)}
        kwargs["http_options"] = options.model_copy(update={"client_args": args})
        kwargs["credentials"] = DelayedCredentials(clock, delay)
        client = original(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(genai, "Client", make)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        original_async(**{**kwargs, "transport": httpx.MockTransport(handle)}))
    return clients


def call(model, clock):
    breaker = job._Breaker(model, call_allowance=lambda: job._call_allowance(clock.now, None, D, FakeChain()))
    return breaker.complete_json(system="offline", user="offline", schema=SCHEMA, model="gemini-3.8-flash",
                                 max_tokens=10)


def test_auth_refresh_cannot_admit_a_provider_post_after_the_deadline(monkeypatch):
    deadline = datetime.combine(D, time(6, 15), SAST)
    clock = SimpleNamespace(now=deadline - timedelta(seconds=5))
    calls = []
    clients = offline_factory(monkeypatch, clock, 10, calls)
    model = GeminiModel(timeout_s=60)
    try:
        with pytest.raises(TimeoutError, match="dispatch deadline"):
            call(model, clock)
        assert clock.now == deadline + timedelta(seconds=5)
        assert calls == []
    finally:
        model.close_deadline_calls()
        for client in clients:
            client.close()


def test_transport_phase_limits_are_recomputed_after_auth_refresh(monkeypatch):
    deadline = datetime.combine(D, time(6, 15), SAST)
    clock = SimpleNamespace(now=deadline - timedelta(seconds=5))
    calls = []
    clients = offline_factory(monkeypatch, clock, 2, calls)
    model = GeminiModel(timeout_s=60)
    try:
        result, usage = call(model, clock)
        assert result == {"answer": "ok"} and usage["usd"] > 0
        assert len(calls) == 1 and calls[0]["at"] < deadline
        assert calls[0]["timeout"] == {"connect": 3.0, "pool": 3.0, "read": 3.0, "write": 3.0}
    finally:
        model.close_deadline_calls()
        for client in clients:
            client.close()


def test_default_calls_keep_existing_timeout_behavior_without_a_dispatch_callback(monkeypatch):
    clock = SimpleNamespace(now=datetime.combine(D, time(6, 14, 55), SAST))
    calls = []
    clients = offline_factory(monkeypatch, clock, 2, calls)
    try:
        result, _ = GeminiModel(timeout_s=60).complete_json(system="offline", user="offline", schema=SCHEMA,
                                                          model="gemini-3.8-flash", max_tokens=10,
                                                          request_timeout_s=5)
        assert result == {"answer": "ok"}
        assert calls[0]["timeout"] == {"connect": 5.0, "pool": 5.0, "read": 5.0, "write": 5.0}
    finally:
        for client in clients:
            client.close()
