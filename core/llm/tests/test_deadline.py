"""Elapsed deadline and cleanup through the installed SDK's public async transport seam."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from google import genai
from google.auth.credentials import Credentials

from core.llm.gemini import GeminiModel
from core.brief import explain, job
from core.brief.tests.test_brief_job import (
    D, EARLY, MARKETS, Client, FakeChain, FakeConfirm, FakeCtx, market_scope_is_valid_by_default, payload, world,
)

BODY = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}, "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}}).encode()
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


class Auth(Credentials):
    def __init__(self):
        super().__init__()
        self.token, self.delay = "offline-token", 0
        self.finished = threading.Event()
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        time.sleep(self.delay)
        self.token = "offline-token"
        self.finished.set()


class Stream(httpx.AsyncByteStream):
    def __init__(self, owner):
        self.owner, self.closed, self.cancelled = owner, False, False

    async def __aiter__(self):
        width = len(BODY) // len(self.owner.body_delays)
        try:
            for i, delay in enumerate(self.owner.body_delays):
                await asyncio.sleep(delay)
                yield BODY[i * width:(i + 1) * width if i + 1 < len(self.owner.body_delays) else len(BODY)]
            self.owner.completed += 1
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.phases, self.body_delays = (), (0,)
        self.dispatched = self.completed = 0
        self.cancelled = self.closed = False
        self.streams = []

    async def handle_async_request(self, request):
        self.dispatched += 1
        try:
            for delay in self.phases:
                await asyncio.sleep(delay)
            stream = Stream(self)
            self.streams.append(stream)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream, request=request)
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self):
        self.closed = True


@pytest.fixture
def native(monkeypatch):
    original_sdk, original_http = genai.Client, httpx.AsyncClient
    auth, transport, clients, http_clients = Auth(), Transport(), [], []

    def async_http(**kwargs):
        kwargs.update(transport=transport, trust_env=False)
        client = original_http(**kwargs)
        http_clients.append(client)
        return client

    def sync_handle(request):
        transport.dispatched += 1
        for delay in (*transport.phases, *transport.body_delays):
            time.sleep(delay)
        transport.completed += 1
        return httpx.Response(200, content=BODY, request=request)

    def sdk(**kwargs):
        options = kwargs["http_options"]
        args = {**(options.client_args or {}), "transport": httpx.MockTransport(sync_handle)}
        kwargs.update(credentials=auth, http_options=options.model_copy(update={"client_args": args}))
        client = original_sdk(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(genai, "Client", sdk)
    monkeypatch.setattr(httpx, "AsyncClient", async_http)
    model = GeminiModel(timeout_s=60)
    yield model, auth, transport, http_clients
    close = getattr(model, "close_deadline_calls", None)
    if close:
        close()
    for client in clients:
        client.close()


def call(model, budget):
    end = time.monotonic() + budget
    return model.complete_json(system="offline", user="offline", schema=SCHEMA, model="gemini-3.8-flash",
                               max_tokens=10, request_timeout_s=budget,
                               request_allowance=lambda: max(0, end - time.monotonic()))


def warm(native):
    model, _, transport, _ = native
    assert call(model, 2)[0] == {"ok": True}
    transport.dispatched = transport.completed = 0
    transport.streams.clear()
    return native


@pytest.mark.parametrize("phases,body,budget", [((0.045, 0.045), (0.02,), 0.07),
                                               ((0.025, 0.025), (0.03, 0.03, 0.03), 0.1)],
                         ids=["cumulative_phases", "cumulative_body"])
def test_elapsed_timeout_acknowledges_request_or_body_cleanup(native, phases, body, budget):
    model, _, transport, _ = warm(native)
    transport.phases, transport.body_delays = phases, body
    with pytest.raises(TimeoutError) as error:
        call(model, budget)
    assert error.value.possibly_dispatched is True
    assert transport.dispatched == 1 and transport.completed == 0
    assert transport.cancelled or any(stream.cancelled for stream in transport.streams)
    assert all(stream.closed for stream in transport.streams)
    time.sleep(sum(phases) + sum(body) + 0.03)
    assert transport.completed == 0


def test_cancelled_auth_retires_owner_and_never_resumes_paid_dispatch(native):
    model, auth, transport, _ = warm(native)
    auth.token, auth.delay = None, 0.2
    started = time.monotonic()
    with pytest.raises(TimeoutError) as error:
        call(model, 0.05)
    assert time.monotonic() - started < 0.15
    assert error.value.possibly_dispatched is False and error.value.auth_unresolved is True
    with pytest.raises(RuntimeError, match="retired"):
        call(model, 1)
    assert auth.finished.wait(1)
    time.sleep(0.03)
    assert auth.refreshes == 1 and transport.dispatched == 0


def test_acknowledged_dispatched_timeout_can_be_followed_by_a_second_call_and_closed(native):
    model, _, transport, clients = warm(native)
    transport.phases = (0.2,)
    with pytest.raises(TimeoutError):
        call(model, 0.05)
    assert transport.cancelled
    transport.phases = ()
    assert call(model, 1)[0] == {"ok": True}
    assert transport.dispatched == 2 and transport.completed == 1
    model.close_deadline_calls()
    assert transport.closed and clients and all(client.is_closed for client in clients)


def run_job(model, *, n=2, chain=None, client=None):
    chain = chain or FakeChain()
    client = client or Client(world(n=n, markets=("ZA",)))
    counts = job.run(client, D, chain=chain, model=model, make_sc=lambda run_id: object(), clock=lambda: EARLY,
                     build_ctx=FakeCtx(), confirm=FakeConfirm(), campaign_hashtags=[],
                     political_terms={m: [] for m in MARKETS}, workers=1, core="core", agent="agent",
                     sleep=lambda seconds: None)
    return counts, chain, client


def test_possibly_dispatched_timeout_books_full_reservation_with_an_unknown_marker(native, monkeypatch):
    model, _, transport, _ = warm(native)
    model.timeout_s = 0.05
    transport.phases = (0.2,)
    estimates = []
    estimate = explain._estimate_usd

    def record_estimate(*args):
        value = estimate(*args)
        estimates.append(value)
        return value

    monkeypatch.setattr(explain, "_estimate_usd", record_estimate)
    counts, chain, client = run_job(model)
    assert len(estimates) == transport.dispatched == 2
    assert counts["model_usd"] == pytest.approx(sum(estimates), abs=0.000001)
    assert counts["model_reserved_usd"] == pytest.approx(sum(estimates), abs=0.000001)
    assert chain.finished[0]["counts"]["model_reserved_usd"] == counts["model_reserved_usd"]
    assert payload(type("R", (), {"client": client})(), "ZA")["held_back"]["count"] == 2


def test_retired_auth_owner_holds_queue_and_terminal_record_precedes_shutdown(native):
    model, auth, transport, _ = warm(native)
    model.timeout_s = 0.05
    auth.token, auth.delay = None, 0.2
    events = []

    class Chain(FakeChain):
        def finish(self, *args, **kwargs):
            events.append("terminal")
            return super().finish(*args, **kwargs)

    close = model.close_deadline_calls

    def owner_close():
        events.append("owner_close")
        return close()

    model.close_deadline_calls = owner_close
    counts, chain, client = run_job(model, chain=Chain())
    assert events == ["terminal", "owner_close"]
    assert auth.finished.is_set() and auth.refreshes == 1 and transport.dispatched == 0
    assert counts["model_usd"] == 0 and counts.get("model_reserved_usd", 0) == 0
    assert counts["model"]["reason"] == "model_unavailable"
    assert payload(type("R", (), {"client": client})(), "ZA")["held_back"]["count"] == 2
    assert chain.finished[0]["status"] == "ok"


def test_cleanup_failure_cannot_replace_a_primary_job_error(native):
    model, _, _, _ = warm(native)
    events = []

    class FailingClient(Client):
        def insert_rows_json(self, table, rows):
            raise RuntimeError("primary job failure")

    class Chain(FakeChain):
        def finish(self, *args, **kwargs):
            events.append("terminal")
            return super().finish(*args, **kwargs)

    close = model.close_deadline_calls

    def failed_close():
        events.append("owner_close")
        close()
        raise RuntimeError("owner cleanup failure")

    model.close_deadline_calls = failed_close
    try:
        with pytest.raises(RuntimeError, match="primary job failure"):
            run_job(model, chain=Chain(), client=FailingClient(world(n=0, markets=("ZA",))))
        assert events == ["terminal", "owner_close"]
    finally:
        model.close_deadline_calls = close


def test_deadline_is_anchored_before_client_preparation(native, monkeypatch):
    model, _, transport, _ = native
    factory = genai.Client

    def slow_factory(**kwargs):
        time.sleep(0.05)
        return factory(**kwargs)

    monkeypatch.setattr(genai, "Client", slow_factory)
    with pytest.raises(TimeoutError) as error:
        call(model, 0.01)
    assert error.value.known_pre_dispatch and not error.value.possibly_dispatched
    assert transport.dispatched == 0


def test_client_initialization_failure_closes_owned_http_and_never_falls_back(native, monkeypatch):
    model, _, transport, clients = native

    def broken_factory(**kwargs):
        raise RuntimeError("synthetic client initialization failure")

    monkeypatch.setattr(genai, "Client", broken_factory)
    with pytest.raises(RuntimeError, match="initialization failure"):
        call(model, 1)
    with pytest.raises(RuntimeError, match="retired"):
        call(model, 1)
    assert clients and all(client.is_closed for client in clients) and transport.dispatched == 0


def test_owner_cleanup_failure_blocks_further_deadline_dispatch(native):
    model, _, transport, _ = warm(native)
    aclose = transport.aclose

    async def failed_close():
        await aclose()
        raise RuntimeError("synthetic transport cleanup failure")

    transport.aclose = failed_close
    try:
        with pytest.raises(RuntimeError, match="cleanup failure"):
            model.close_deadline_calls()
        with pytest.raises(RuntimeError, match="retired"):
            call(model, 1)
        assert transport.dispatched == 0
    finally:
        transport.aclose = aclose


def test_loop_start_failure_retires_owner_without_a_later_dispatch_or_hanging_shutdown(native, monkeypatch):
    model, _, transport, _ = native

    def failed_start(self):
        raise RuntimeError("synthetic loop startup failure")

    monkeypatch.setattr(threading.Thread, "start", failed_start)
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            call(model, 1)
        assert model._deadline_runner.retired
        with pytest.raises(RuntimeError, match="retired"):
            call(model, 1)
        model.close_deadline_calls()
        assert transport.dispatched == 0
    finally:
        runner = model._deadline_runner
        if runner and runner._loop and not runner._loop.is_running() and not runner._thread.is_alive():
            runner._loop.close()
            runner._loop = None


@pytest.mark.parametrize("cause", ["elapsed", "read_timeout", "read_timeout_explicit_cause"])
def test_cleanup_cannot_mask_timeout_causality_or_its_reservation(native, monkeypatch, cause):
    model, _, transport, _ = warm(native)
    transport.body_delays = (0.2,)

    async def failed_aclose(self):
        if cause == "read_timeout_explicit_cause":
            raise RuntimeError("synthetic response cleanup failure") from ValueError("synthetic close detail")
        raise RuntimeError("synthetic response cleanup failure")

    async def failed_read(self):
        yield BODY[:1]
        raise httpx.ReadTimeout("synthetic body read timeout")

    monkeypatch.setattr(Stream, "aclose", failed_aclose)
    if cause.startswith("read_timeout"):
        monkeypatch.setattr(Stream, "__aiter__", failed_read)
    with pytest.raises(TimeoutError) as error:
        call(model, 0.05 if cause == "elapsed" else 1)
    assert error.value.possibly_dispatched and error.value.reserve_model_estimate
    assert error.value.request_cleanup_failed
    assert isinstance(error.value.__cause__, RuntimeError)
    assert transport.dispatched == 1 and transport.completed == 0
    if cause == "read_timeout_explicit_cause":
        assert isinstance(error.value.__cause__.__cause__, ValueError)
        assert isinstance(error.value.__cause__.__context__, httpx.ReadTimeout)
    with pytest.raises(RuntimeError, match="retired"):
        call(model, 1)
    assert transport.dispatched == 1


def test_failed_deadline_response_cleanup_refuses_a_second_dispatch(native, monkeypatch):
    model, _, transport, _ = warm(native)
    transport.body_delays = (0.2,)
    aclose = Stream.aclose

    async def failed_aclose(self):
        raise RuntimeError("synthetic response cleanup failure")

    monkeypatch.setattr(Stream, "aclose", failed_aclose)
    with pytest.raises((RuntimeError, TimeoutError)):
        call(model, 0.05)
    monkeypatch.setattr(Stream, "aclose", aclose)
    transport.body_delays = (0,)
    with pytest.raises(RuntimeError, match="retired"):
        call(model, 1)
    assert transport.dispatched == 1


def test_masked_deadline_cleanup_books_the_quote_once_and_holds_the_remaining_queue(native, monkeypatch):
    model, _, transport, _ = warm(native)
    model.timeout_s = 0.05
    transport.body_delays = (0.2,)
    estimates = []
    estimate = explain._estimate_usd

    def record_estimate(*args):
        value = estimate(*args)
        estimates.append(value)
        return value

    async def failed_aclose(self):
        raise RuntimeError("synthetic response cleanup failure")

    monkeypatch.setattr(explain, "_estimate_usd", record_estimate)
    monkeypatch.setattr(Stream, "aclose", failed_aclose)
    counts, chain, client = run_job(model)
    assert len(estimates) == transport.dispatched == 1
    assert counts["model_usd"] == pytest.approx(estimates[0], abs=0.000001)
    assert counts["model_reserved_usd"] == counts["model_usd"]
    assert counts["model"]["reason"] == "model_unavailable"
    assert chain.finished[0]["counts"]["model_reserved_usd"] == counts["model_reserved_usd"]
    assert payload(type("R", (), {"client": client})(), "ZA")["held_back"]["count"] == 2


def test_unrelated_cleanup_error_before_expiry_keeps_ordinary_error_semantics(native, monkeypatch):
    model, _, transport, _ = warm(native)
    aclose = Stream.aclose

    async def failed_aclose(self):
        raise RuntimeError("ordinary synthetic cleanup failure")

    monkeypatch.setattr(Stream, "aclose", failed_aclose)
    with pytest.raises(RuntimeError, match="ordinary synthetic cleanup failure") as error:
        call(model, 1)
    assert not getattr(error.value, "reserve_model_estimate", False)
    monkeypatch.setattr(Stream, "aclose", aclose)
    assert call(model, 1)[0] == {"ok": True}
    assert transport.dispatched == 2
