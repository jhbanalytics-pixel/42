"""f42-api against fake store, today and agent modules (contract.md sections 1 to 6)."""

import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

import core.api
from core.api import app as api_mod
from core.api import auth

# Arithmetic against the cap as it stood until 3 October 2026 (conftest.py).
pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

PASS = "s3cret-passcode"

@pytest.fixture(autouse=True)
def fresh_health_checks():
    """Each test asks the health route for checks of its own, not those an earlier test cached."""
    from core.api import app as api_app
    api_app._reset_health_cache()
    yield
    api_app._reset_health_cache()

GOOD = {"X-Passcode": PASS}


class NotReady(Exception):
    pass


class NotFound(Exception):
    pass


class FakeStore:
    def __init__(self, records):
        self.records = records

    def health(self):
        return "ok"

    def ask_record(self, ask_id):
        return self.records.get(ask_id)


def make_today(calls):
    def build_today(store, date=None):
        calls.append(("today", date))
        if date == "2026-09-01":
            raise NotReady("no brief")
        return {"date": date or "2026-09-30", "status": "published", "markets": []}

    def build_trends(store, market, date=None):
        calls.append(("trends", market, date))
        if date == "2026-09-01":
            raise NotReady("no brief")
        if date == "2026-09-02":
            raise NotFound(market)
        return {"date": "2026-09-30", "market": market, "cards": []}

    def build_trend(store, item_id, market, date=None):
        calls.append(("trend", item_id, market, date))
        if date == "2026-09-01":
            raise NotReady("no brief")
        if item_id != "known":
            raise NotFound(item_id)
        return {"item_id": item_id, "market": market}

    def today_status(store, date):
        calls.append(("status", date))
        return "published"

    return types.SimpleNamespace(
        NotReady=NotReady,
        NotFound=NotFound,
        build_today=build_today,
        build_trends=build_trends,
        build_trend=build_trend,
        today_status=today_status,
    )


SSE_BODY = 'id: {n}\nevent: step\ndata: {{"seq": {n}, "text": "after {lei}"}}\n\n'


def make_agent(seen):
    agent = FastAPI()

    @agent.get("/health")
    async def health():
        return {"ok": True}

    @agent.post("/api/ask", status_code=202)
    async def start(request: Request):
        body = await request.json()
        seen.setdefault("posts", []).append(body)
        seen["authorization"] = request.headers.get("authorization")
        if body.get("question") == "no":
            return JSONResponse({"error": "bad_request", "message": "too short"}, status_code=400)
        return {
            "ask_id": "a_1",
            "status": "running",
            "events_url": "/api/ask/a_1/events",
            "url": "/api/ask/a_1",
        }

    @agent.get("/api/ask/{ask_id}")
    async def read(ask_id: str):
        if ask_id == "a_1":
            return {"ask_id": "a_1", "status": "running", "answer": None}
        return JSONResponse({"error": "not_found", "message": "not held"}, status_code=404)

    @agent.get("/api/ask/{ask_id}/events")
    async def events(ask_id: str, request: Request):
        if ask_id != "a_1":
            return JSONResponse({"error": "not_found", "message": "not held"}, status_code=404)
        lei = request.headers.get("last-event-id", "none")

        async def gen():
            yield SSE_BODY.format(n=4, lei=lei)
            yield 'id: 5\nevent: done\ndata: {"seq": 5, "status": "complete", "url": "/api/ask/a_1"}\n\n'

        return StreamingResponse(gen(), media_type="text/event-stream")

    @agent.post("/api/ask/{ask_id}/stop", status_code=202)
    async def stop(ask_id: str):
        return {"ask_id": ask_id, "status": "stopping"}

    @agent.get("/api/watches")
    async def list_watches():
        if seen.get("watches_status"):
            return JSONResponse({"error": "internal", "message": "no"}, status_code=seen["watches_status"])
        return {"watches": seen.get("watches", [])}

    @agent.post("/api/watches", status_code=201)
    async def create_watch(request: Request):
        body = await request.json()
        seen.setdefault("watch_posts", []).append(body)
        if body.get("market") == "XX":
            return JSONResponse({"error": "bad_request", "message": "market"}, status_code=400)
        return {"watch_id": "w_0123456789ab", **body, "status": "active"}

    @agent.post("/api/watches/{watch_id}/{action}")
    async def watch_action(watch_id: str, action: str):
        seen.setdefault("watch_actions", []).append((watch_id, action))
        if watch_id == "w_missing":
            return JSONResponse({"error": "not_found", "message": "no watch"}, status_code=404)
        return {"watch_id": watch_id, "status": "paused" if action == "pause" else "active"}

    @agent.post("/api/feedback", status_code=202)
    async def feedback(request: Request):
        seen.setdefault("feedback", []).append(await request.json())
        return {"ok": True}

    return agent


STORED = {
    "a_old": {
        "ask_id": "a_old",
        "status": "complete",
        "answer": {"status": "complete"},
        "steps": [
            {"seq": 1, "kind": "plan", "text": "Planning"},
            {"seq": 2, "kind": "search", "text": "Reading TikTok: 12 found"},
        ],
    }
}


@pytest.fixture(autouse=True)
def nobody_hidden(monkeypatch):
    """Readers now ask who is hidden. These tests are about other things and their stores hold no suppression list;
    the readers' own behaviour is pinned in test_privacy_*.py."""
    from core.api import privacy
    monkeypatch.setattr(privacy, "read_hidden", lambda store: (set(), set(), set()))


@pytest.fixture
def ctx(monkeypatch):
    seen, calls = {}, []
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.delenv("F42_AUTH_MODE", raising=False)
    monkeypatch.delenv("IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("IAP_ALLOWED_EMAILS", raising=False)
    monkeypatch.setenv("F42_VERSION", "abc123")
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("MODEL_DAILY_USD", raising=False)
    monkeypatch.delenv("ASK_PER_IP_DAILY", raising=False)
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    store = FakeStore(dict(STORED))
    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: store), raising=False
    )
    monkeypatch.setattr(core.api, "today", make_today(calls), raising=False)
    monkeypatch.setattr(
        core.api, "agent_app", types.SimpleNamespace(app=make_agent(seen)), raising=False
    )
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()
    return types.SimpleNamespace(
        client=TestClient(api_mod.app), seen=seen, calls=calls, store=store
    )


def remote_agent(monkeypatch, transport):
    """Point AGENT_URL at a remote agent and route httpx to `transport`."""
    monkeypatch.setenv("AGENT_URL", "https://f42-agent.example.run.app")
    monkeypatch.setattr(api_mod, "_id_token", lambda audience: f"idtok-for-{audience}")
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)


def refused(request):
    raise httpx.ConnectError("connection refused", request=request)


# Health


def test_health_needs_no_passcode_and_reports_checks(ctx):
    r = ctx.client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "f42-api"
    assert body["version"] == "abc123"
    assert body["time"].endswith("+02:00")
    assert body["auth_mode"] == "passcode"
    assert body["passcode"] is True
    assert isinstance(body["model_daily_cap_usd"], float)
    assert body["checks"] == {
        "auth": "ok",
        "bigquery": "ok",
        "agent": "ok",
        "today": "published",
    }
    assert ctx.calls[-1][0] == "status"


def test_health_model_cap_uses_same_sast_instant_as_time(ctx, monkeypatch):
    from datetime import datetime
    from core.config import caps as caps_config

    health_time = datetime(2026, 10, 2, 23, 59, 59, tzinfo=auth.SAST)
    monkeypatch.setattr(api_mod, "datetime", types.SimpleNamespace(now=lambda tz: health_time))
    seen = []

    def shared_cap(*, now):
        seen.append(now)
        return 80.0

    monkeypatch.setattr(caps_config, "model_daily_usd", shared_cap)

    r = ctx.client.get("/api/health")
    body = r.json()

    assert r.status_code == 200
    assert body["model_daily_cap_usd"] == 80.0
    assert body["time"] == health_time.isoformat(timespec="seconds")
    assert seen == [health_time]


def test_health_model_cap_uses_the_sast_date_at_midnight(ctx, monkeypatch):
    from datetime import datetime, timezone
    from core.config import caps as caps_config

    instants = (
        datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc),
        datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc),
    )
    readbacks = []
    for instant, expected_day in zip(instants, ("2026-10-02", "2026-10-03")):
        monkeypatch.setattr(
            api_mod, "datetime", types.SimpleNamespace(now=lambda tz, value=instant: value.astimezone(tz))
        )
        r = ctx.client.get("/api/health")
        body = r.json()
        health_time = datetime.fromisoformat(body["time"])

        assert r.status_code == 200
        assert health_time.date().isoformat() == expected_day
        assert body["model_daily_cap_usd"] == caps_config.model_daily_usd(now=health_time)
        readbacks.append(body["model_daily_cap_usd"])

    assert readbacks[0] != readbacks[1]


def test_health_model_cap_override_only_lowers_shared_cap(ctx, monkeypatch):
    from core.config import caps as caps_config

    monkeypatch.setattr(caps_config, "model_daily_usd", lambda *, now: 80.0)
    for override, expected in (("40", 40.0), ("100", 80.0)):
        monkeypatch.setenv("MODEL_DAILY_USD", override)
        body = ctx.client.get("/api/health").json()
        assert body["model_daily_cap_usd"] == expected


def test_health_cap_read_failure_returns_no_success_body(ctx, monkeypatch):
    from core.config import caps as caps_config

    monkeypatch.setattr(caps_config, "model_daily_usd", lambda *, now: (_ for _ in ()).throw(OSError("cap read failed")))
    response = TestClient(api_mod.app, raise_server_exceptions=False).get("/api/health")

    assert response.status_code == 500
    assert response.json()["error"] == "internal"
    assert "ok" not in response.json()


def test_health_invalid_cap_override_returns_no_success_body(ctx, monkeypatch):
    monkeypatch.setenv("MODEL_DAILY_USD", "not-a-number")
    response = TestClient(api_mod.app, raise_server_exceptions=False).get("/api/health")

    assert response.status_code == 500
    assert response.json()["error"] == "internal"
    assert "ok" not in response.json()


def test_health_version_defaults_to_dev(ctx, monkeypatch):
    monkeypatch.delenv("F42_VERSION")
    assert ctx.client.get("/api/health").json()["version"] == "dev"


def test_health_ok_false_but_200_when_agent_unreachable(ctx, monkeypatch):
    remote_agent(monkeypatch, httpx.MockTransport(refused))
    r = ctx.client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["agent"] != "ok"
    assert body["checks"]["bigquery"] == "ok"


def agent_health(body, status=200):
    def handler(request):
        assert request.url.path == "/health"
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_health_t2_ready_is_false_when_the_agent_does_not_say_so(ctx):
    assert ctx.client.get("/api/health").json()["t2_ready"] is False


def test_health_t2_ready_follows_the_agent(ctx, monkeypatch):
    remote_agent(monkeypatch, agent_health({"ok": True, "t2_ready": True}))
    body = ctx.client.get("/api/health").json()
    assert body["checks"]["agent"] == "ok"
    assert body["t2_ready"] is True


@pytest.mark.parametrize("answer, status", [
    ({"ok": True, "t2_ready": False}, 200),
    ({"ok": True, "t2_ready": "1"}, 200),
    ({"ok": True, "t2_ready": 1}, 200),
    ({"ok": False, "t2_ready": True}, 503),
])
def test_health_t2_ready_fails_closed(ctx, monkeypatch, answer, status):
    remote_agent(monkeypatch, agent_health(answer, status))
    assert ctx.client.get("/api/health").json()["t2_ready"] is False


def test_health_t2_ready_is_false_when_the_agent_is_unreachable(ctx, monkeypatch):
    remote_agent(monkeypatch, httpx.MockTransport(refused))
    assert ctx.client.get("/api/health").json()["t2_ready"] is False


# Gate


def test_gate_rejects_missing_and_wrong_passcode(ctx):
    for headers in ({}, {"X-Passcode": "wrong"}, {"Authorization": "Bearer wrong"}):
        r = ctx.client.get("/api/today", headers=headers)
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"
        assert r.json()["message"]


def test_gate_fails_closed_when_passcode_unset(ctx, monkeypatch):
    monkeypatch.delenv("UI_PASSCODE")
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    r = ctx.client.get("/api/today", headers=GOOD)
    assert r.status_code == 503
    assert r.json()["error"] == "gate_not_configured"


def test_gate_passes_with_x_passcode_and_with_bearer(ctx):
    assert ctx.client.get("/api/today", headers=GOOD).status_code == 200
    bearer = {"Authorization": f"Bearer {PASS}"}
    assert ctx.client.get("/api/today", headers=bearer).status_code == 200


# Auth verify


def test_auth_verify_ok_and_401(ctx):
    r = ctx.client.post("/api/auth/verify", json={"passcode": PASS})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = ctx.client.post("/api/auth/verify", json={"passcode": "nope"})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_auth_verify_503_when_passcode_unset(ctx, monkeypatch):
    monkeypatch.delenv("UI_PASSCODE")
    r = ctx.client.post("/api/auth/verify", json={"passcode": "x"})
    assert r.status_code == 503 and r.json()["error"] == "gate_not_configured"


def test_auth_verify_rate_limit_keys_on_rightmost_forwarded_hop(ctx):
    for i in range(10):
        hdr = {"X-Forwarded-For": f"9.9.9.{i}, 10.0.0.1"}
        r = ctx.client.post("/api/auth/verify", json={"passcode": "nope"}, headers=hdr)
        assert r.status_code == 401
    hdr = {"X-Forwarded-For": "8.8.8.8, 10.0.0.1"}
    r = ctx.client.post("/api/auth/verify", json={"passcode": PASS}, headers=hdr)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    other = {"X-Forwarded-For": "10.0.0.2"}
    r = ctx.client.post("/api/auth/verify", json={"passcode": PASS}, headers=other)
    assert r.status_code == 200


def test_wrong_passcodes_on_gated_routes_hit_the_auth_limit(ctx):
    guesser = {"X-Forwarded-For": "41.1.1.1"}
    for _ in range(10):
        r = ctx.client.get("/api/today", headers={**guesser, "X-Passcode": "wrong"})
        assert r.status_code == 401
    r = ctx.client.get("/api/today", headers={**guesser, "X-Passcode": "wrong"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    r = ctx.client.get("/api/today", headers={**guesser, "X-Passcode": PASS})
    assert r.status_code == 429, "an over-limit IP must not learn a right guess"
    r = ctx.client.post("/api/auth/verify", json={"passcode": "nope"}, headers=guesser)
    assert r.status_code == 429
    other = {"X-Forwarded-For": "41.2.2.2", "X-Passcode": PASS}
    assert ctx.client.get("/api/today", headers=other).status_code == 200


def test_right_passcodes_on_gated_routes_do_not_use_the_auth_limit(ctx):
    for _ in range(25):
        assert ctx.client.get("/api/today", headers=GOOD).status_code == 200
    r = ctx.client.post("/api/auth/verify", json={"passcode": PASS})
    assert r.status_code == 200


# Today and trends


def test_today_200_passes_date(ctx):
    r = ctx.client.get("/api/today", params={"date": "2026-09-30"}, headers=GOOD)
    assert r.status_code == 200
    assert r.json()["date"] == "2026-09-30"
    assert ("today", "2026-09-30") in ctx.calls
    assert ctx.client.get("/api/today", headers=GOOD).status_code == 200
    assert ("today", None) in ctx.calls


def test_today_409_not_ready(ctx):
    r = ctx.client.get("/api/today", params={"date": "2026-09-01"}, headers=GOOD)
    assert r.status_code == 409
    assert r.json()["error"] == "not_ready"


def test_today_400_on_bad_date(ctx):
    r = ctx.client.get("/api/today", params={"date": "30-09-2026"}, headers=GOOD)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_trends_400_on_bad_or_missing_market(ctx):
    for params in ({"market": "XX"}, {}):
        r = ctx.client.get("/api/trends", params=params, headers=GOOD)
        assert r.status_code == 400
        assert r.json()["error"] == "bad_request"


def test_trends_200(ctx):
    r = ctx.client.get("/api/trends", params={"market": "ZA"}, headers=GOOD)
    assert r.status_code == 200
    assert r.json()["market"] == "ZA"
    assert ("trends", "ZA", None) in ctx.calls


def test_trend_404_and_200(ctx):
    r = ctx.client.get("/api/trends/missing", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    r = ctx.client.get(
        "/api/trends/known", params={"market": "KE", "date": "2026-09-30"}, headers=GOOD
    )
    assert r.status_code == 200
    assert ("trend", "known", "KE", "2026-09-30") in ctx.calls


@pytest.mark.parametrize("path", ["/api/trends", "/api/trends/known"])
def test_trends_409_not_ready(ctx, path):
    r = ctx.client.get(path, params={"market": "ZA", "date": "2026-09-01"}, headers=GOOD)
    assert r.status_code == 409
    assert r.json()["error"] == "not_ready"
    assert r.json()["message"]


@pytest.mark.parametrize("path", ["/api/trends", "/api/trends/missing"])
def test_trends_404_not_found(ctx, path):
    params = {"market": "ZA"}
    if path == "/api/trends":
        params["date"] = "2026-09-02"
    r = ctx.client.get(path, params=params, headers=GOOD)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert r.json()["message"]


# Ask forwarding


def test_ask_post_forwarded_returns_202(ctx):
    body = {"question": "What is behind #example in South Africa?", "market": "ZA"}
    r = ctx.client.post("/api/ask", json=body, headers=GOOD)
    assert r.status_code == 202
    assert r.json()["ask_id"] == "a_1"
    assert r.json()["events_url"] == "/api/ask/a_1/events"
    assert ctx.seen["posts"] == [body]
    assert ctx.seen["authorization"] is None


def test_ask_needs_passcode(ctx):
    r = ctx.client.post("/api/ask", json={"question": "abc"})
    assert r.status_code == 401
    assert "posts" not in ctx.seen


def test_ask_remote_agent_gets_id_token_not_passcode(ctx, monkeypatch):
    seen = ctx.seen
    remote_agent(monkeypatch, httpx.ASGITransport(app=make_agent(seen)))
    bearer = {"Authorization": f"Bearer {PASS}"}
    r = ctx.client.post("/api/ask", json={"question": "abc def"}, headers=bearer)
    assert r.status_code == 202
    assert seen["authorization"] == "Bearer idtok-for-https://f42-agent.example.run.app"


def test_ask_502_when_agent_unreachable(ctx, monkeypatch):
    remote_agent(monkeypatch, httpx.MockTransport(refused))
    r = ctx.client.post("/api/ask", json={"question": "abc def"}, headers=GOOD)
    assert r.status_code == 502
    assert r.json()["error"] == "agent_unavailable"
    r = ctx.client.get("/api/ask/a_1", headers=GOOD)
    assert r.status_code == 502


def test_ask_events_stream_passes_through_with_last_event_id(ctx):
    r = ctx.client.get("/api/ask/a_1/events", headers={**GOOD, "Last-Event-ID": "3"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    expected = SSE_BODY.format(n=4, lei="3") + (
        'id: 5\nevent: done\ndata: {"seq": 5, "status": "complete", "url": "/api/ask/a_1"}\n\n'
    )
    assert r.text == expected


def test_ask_events_replay_stored_record_when_agent_no_longer_holds_it(ctx):
    r = ctx.client.get("/api/ask/a_old/events", headers={**GOOD, "Last-Event-ID": "1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    blocks = [b for b in r.text.split("\n\n") if b.strip()]
    assert blocks[0].startswith("id: 2\nevent: step\ndata: ")
    assert json.loads(blocks[0].split("data: ", 1)[1])["text"] == "Reading TikTok: 12 found"
    assert blocks[-1].startswith("id: 3\nevent: done\ndata: ")
    done = json.loads(blocks[-1].split("data: ", 1)[1])
    assert done == {"seq": 3, "status": "complete", "url": "/api/ask/a_old"}
    r = ctx.client.get("/api/ask/a_gone/events", headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_ask_get_forwarded_and_falls_back_to_store(ctx):
    r = ctx.client.get("/api/ask/a_1", headers=GOOD)
    assert r.status_code == 200 and r.json()["status"] == "running"
    r = ctx.client.get("/api/ask/a_old", headers=GOOD)
    assert r.status_code == 200
    assert r.json() == {**STORED["a_old"], "answer_meta": {"check": "legacy_unknown"}}  # a record with no typed state
    r = ctx.client.get("/api/ask/a_gone", headers=GOOD)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_ask_id_with_odd_characters_is_400(ctx):
    r = ctx.client.get("/api/ask/a%3Bdrop", headers=GOOD)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_ask_stop_forwarded(ctx):
    r = ctx.client.post("/api/ask/a_1/stop", headers=GOOD)
    assert r.status_code == 202
    assert r.json() == {"ask_id": "a_1", "status": "stopping"}


def test_ask_body_must_be_json_object(ctx):
    r = ctx.client.post("/api/ask", content=b"[1, 2]", headers={**GOOD, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


# Ask limits


def test_daily_limit_counts_live_asks_only_and_resets_by_sast_date(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "2")
    live = {"question": "abc def", "mode": "live"}
    replay = {"question": "abc def", "mode": "replay"}
    assert ctx.client.post("/api/ask", json=live, headers=GOOD).status_code == 202
    assert ctx.client.post("/api/ask", json={"question": "abc def"}, headers=GOOD).status_code == 202
    r = ctx.client.post("/api/ask", json=live, headers=GOOD)
    assert r.status_code == 429
    assert r.json()["error"] == "daily_question_limit"
    assert len(ctx.seen["posts"]) == 2
    assert ctx.client.post("/api/ask", json=replay, headers=GOOD).status_code == 202
    other_ip = {**GOOD, "X-Forwarded-For": "41.0.0.9"}
    assert ctx.client.post("/api/ask", json=live, headers=other_ip).status_code == 202
    monkeypatch.setattr(auth, "sast_date", lambda: "2099-01-01")
    assert ctx.client.post("/api/ask", json=live, headers=GOOD).status_code == 202


def test_daily_limit_does_not_count_asks_the_agent_rejects(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    r = ctx.client.post("/api/ask", json={"question": "no"}, headers=GOOD)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    assert ctx.client.post("/api/ask", json={"question": "abc def"}, headers=GOOD).status_code == 202


def test_ask_rate_limit_30_per_10_seconds(ctx):
    replay = {"question": "abc def", "mode": "replay"}
    for _ in range(30):
        assert ctx.client.post("/api/ask", json=replay, headers=GOOD).status_code == 202
    r = ctx.client.post("/api/ask", json=replay, headers=GOOD)
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


# Static app


@pytest.fixture
def dist(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>42 app</title>", encoding="utf-8")
    (tmp_path / "assets" / "app-abc.js").write_text("console.log(42)", encoding="utf-8")
    monkeypatch.setenv("WEB_DIST", str(tmp_path))
    return tmp_path


def test_spa_fallback_serves_index_for_app_paths(ctx, dist):
    for path in ("/", "/today", "/ask/a_1", "/trends/ZA"):
        r = ctx.client.get(path)
        assert r.status_code == 200, path
        assert "42 app" in r.text
        assert r.headers["content-type"].startswith("text/html")


def test_assets_served_immutable(dist):
    # The /assets mount is made at import only when the build exists, as in the
    # lifted app, so the class is checked on its own mount here.
    from starlette.applications import Starlette
    from starlette.routing import Mount

    static = api_mod.CachedStaticFiles(directory=dist / "assets")
    r = TestClient(Starlette(routes=[Mount("/assets", app=static)])).get("/assets/app-abc.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_top_level_dist_file_served_and_no_escape(ctx, dist):
    (dist / "favicon.ico").write_bytes(b"ico")
    assert ctx.client.get("/favicon.ico").content == b"ico"
    r = ctx.client.get("/..%2F..%2Fsecret.txt")
    assert "42 app" in r.text


def test_unknown_api_path_is_json_404(ctx, dist):
    for method in ("get", "post"):
        r = getattr(ctx.client, method)("/api/nope")
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


def test_placeholder_page_when_dist_missing(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_DIST", str(tmp_path / "missing"))
    r = ctx.client.get("/today")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "42" in r.text


# Export (contract section 6) and the local agent path.

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_export_stored_record_downloads_html(ctx):
    record = json.loads((FIXTURES / "ask_complete.json").read_text(encoding="utf-8"))
    ctx.store.records["a_done"] = record
    r = ctx.client.get("/api/ask/a_done/export?format=html", headers=GOOD)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == 'attachment; filename="42-answer-a_done.html"'
    assert record["question"] in r.text


def test_export_running_record_is_refused(ctx):
    r = ctx.client.get("/api/ask/a_1/export", headers=GOOD)
    assert r.status_code == 409
    assert r.json()["error"] == "not_ready"


def test_export_needs_passcode_and_html_format(ctx):
    assert ctx.client.get("/api/ask/a_1/export").status_code == 401
    r = ctx.client.get("/api/ask/a_1/export?format=pdf", headers=GOOD)
    assert r.status_code == 400


def test_local_agent_url_skips_id_token(ctx, monkeypatch):
    monkeypatch.setenv("AGENT_URL", "http://127.0.0.1:8081")

    def no_token(audience):
        raise AssertionError("no ID token for a local agent")

    monkeypatch.setattr(api_mod, "_id_token", no_token)
    transport = httpx.ASGITransport(app=make_agent(ctx.seen))
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    r = ctx.client.post("/api/ask", json={"question": "What is new?"}, headers=GOOD)
    assert r.status_code == 202
    assert ctx.seen["authorization"] is None


def test_right_sign_ins_from_one_office_ip_never_lock_it_out(ctx):
    office = {"X-Forwarded-For": "196.1.1.1"}
    for _ in range(15):
        r = ctx.client.post("/api/auth/verify", json={"passcode": PASS}, headers=office)
        assert r.status_code == 200
    r = ctx.client.get("/api/today", headers={**office, "X-Passcode": PASS})
    assert r.status_code == 200


# Stage 2 read routes (contract section 10): Discover, Radar, topic pages, Coverage, over the real fixtures.

STEP_ID = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"


@pytest.fixture
def v2(ctx, monkeypatch):
    from core.api.store import FixtureStore

    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: FixtureStore()), raising=False
    )
    monkeypatch.setattr(auth, "sast_date", lambda: "2026-09-30")
    return ctx


@pytest.mark.parametrize(
    "path",
    ["/api/discover?market=ZA", "/api/discover/radar?market=ZA",
     f"/api/topics/{STEP_ID}?market=ZA", "/api/coverage"],
)
def test_v2_routes_need_the_passcode(v2, path):
    assert v2.client.get(path).status_code == 401
    assert v2.client.get(path, headers=GOOD).status_code == 200


def test_discover_route(v2):
    r = v2.client.get("/api/discover", params={"market": "za", "sort": "reach", "limit": 2}, headers=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "ZA" and len(body["items"]) == 2 and body["next_cursor"] == "2"
    assert body["items"][0]["reach"]["value"] == 31
    assert body["held_back"]["count"] == 6
    r = v2.client.get("/api/discover", params={"market": "all"}, headers=GOOD)
    assert r.status_code == 200 and r.json()["market"] == "all"


def test_today_and_discover_routes_carry_searching_now_rows(v2, monkeypatch):
    """contract.md section 19: both envelopes carry the list, each row exactly the five fields, Discover's in its
    market only. The fixture rows are dated, so which ones show depends on the day the test runs."""
    monkeypatch.setattr(core.api, "today", sys.modules["core.api.today"], raising=False)  # ctx stubs Today
    fields = {"term", "market", "source", "rank", "refreshed_at"}
    today_rows = v2.client.get("/api/today", params={"date": "2026-09-30"}, headers=GOOD).json()["searching_now"]
    assert [r["market"] for r in today_rows] == ["ZA", "ZA", "ZA", "NG"]
    assert all(set(r) == fields for r in today_rows)
    for market in ("ZA", "NG", "KE", "all"):
        body = v2.client.get("/api/discover", params={"market": market}, headers=GOOD).json()
        rows = body["searching_now"]
        assert isinstance(rows, list) and all(set(r) == fields for r in rows)
        assert market == "all" or all(r["market"] == market for r in rows)


@pytest.mark.parametrize(
    "params",
    [{}, {"market": "XX"}, {"market": "ZA", "sort": "hot"}, {"market": "ZA", "limit": 0},
     {"market": "ZA", "limit": "many"}, {"market": "ZA", "cursor": "abc"}],
)
def test_discover_route_400(v2, params):
    r = v2.client.get("/api/discover", params=params, headers=GOOD)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request" and r.json()["message"]


def test_radar_route(v2):
    r = v2.client.get("/api/discover/radar", params={"market": "ZA", "kind": "hashtag"}, headers=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["note"] == "Growth needs 14 days of data; showing reach only"
    assert body["held_back_count"] == 4
    assert v2.client.get("/api/discover/radar", headers=GOOD).status_code == 400


def test_topic_route(v2):
    r = v2.client.get(f"/api/topics/{STEP_ID}", params={"market": "ZA"}, headers=GOOD)
    assert r.status_code == 200
    assert r.json()["card"]["item_id"] == STEP_ID
    r = v2.client.get("/api/topics/unknown_item", params={"market": "ZA"}, headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    for params in ({}, {"market": "all"}):
        assert v2.client.get(f"/api/topics/{STEP_ID}", params=params, headers=GOOD).status_code == 400
    assert v2.client.get("/api/topics/bad%20id!", params={"market": "ZA"}, headers=GOOD).status_code == 400


def test_v2_routes_409_before_the_first_detect_run(v2, monkeypatch, tmp_path):
    from core.api.store import FixtureStore

    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: FixtureStore(root=tmp_path)), raising=False
    )
    for path in ("/api/discover?market=ZA", "/api/discover/radar?market=ZA", f"/api/topics/{STEP_ID}?market=ZA"):
        r = v2.client.get(path, headers=GOOD)
        assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_coverage_route(v2):
    r = v2.client.get("/api/coverage", headers=GOOD)
    assert r.status_code == 200
    assert r.json()["date"] == "2026-09-30"  # SAST today by default
    assert r.json()["credits"]["total"]["value"] == 310.0
    r = v2.client.get("/api/coverage", params={"date": "2026-09-29"}, headers=GOOD)
    assert r.status_code == 200 and r.json()["date"] == "2026-09-29"
    r = v2.client.get("/api/coverage", params={"date": "29-09-2026"}, headers=GOOD)
    assert r.status_code == 400


# Watches, feedback and alerts (contract section 10.5 and 10.6): writes forward to f42-agent.

RISING_ID = "4b942b2e"  # prefix of the fixture's ZA rising topic, emerging on 29 September


def rising_id():
    from core.api.store import FixtureStore

    rows = FixtureStore().item_states({"run_id": "r_detect_20260930_01", "run_date": "2026-09-30"}, "ZA")
    return next(r["item_id"] for r in rows if r["item_id"].startswith(RISING_ID))


WATCH_BODY = {"target": {"kind": "hashtag", "value": "#amapiano"}, "market": "ZA",
              "rule": {"state_in": ["rising"]}, "label": "Amapiano"}
FEEDBACK_BODY = {"target": {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-09-30"},
                 "value": "real"}


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/watches"), ("POST", "/api/watches"), ("POST", "/api/watches/w_0123456789ab/pause"),
    ("POST", "/api/watches/w_0123456789ab/resume"), ("POST", "/api/feedback"), ("GET", "/api/alerts"),
])
def test_watch_feedback_and_alert_routes_need_the_passcode(v2, method, path):
    assert v2.client.request(method, path, json={}).status_code == 401
    assert v2.seen.get("watch_posts") is None and v2.seen.get("feedback") is None


def test_watch_routes_forward_to_the_agent(v2):
    r = v2.client.post("/api/watches", json=WATCH_BODY, headers=GOOD)
    assert r.status_code == 201 and r.json()["watch_id"] == "w_0123456789ab"
    assert v2.seen["watch_posts"] == [WATCH_BODY]
    r = v2.client.post("/api/watches", json={**WATCH_BODY, "market": "XX"}, headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    v2.seen["watches"] = [{"watch_id": "w_0123456789ab", "status": "active"}]
    r = v2.client.get("/api/watches", headers=GOOD)
    assert r.status_code == 200 and r.json() == {"watches": v2.seen["watches"]}
    for action, status in (("pause", "paused"), ("resume", "active")):
        r = v2.client.post(f"/api/watches/w_0123456789ab/{action}", headers=GOOD)
        assert r.status_code == 200 and r.json()["status"] == status
    assert v2.seen["watch_actions"] == [("w_0123456789ab", "pause"), ("w_0123456789ab", "resume")]
    r = v2.client.post("/api/watches/w_missing/pause", headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_watch_id_is_checked_like_an_ask_id(v2):
    for action in ("pause", "resume"):
        r = v2.client.post(f"/api/watches/bad%20id!/{action}", headers=GOOD)
        assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert v2.seen.get("watch_actions") is None


def test_feedback_forwards_and_returns_202(v2):
    r = v2.client.post("/api/feedback", json=FEEDBACK_BODY, headers=GOOD)
    assert r.status_code == 202 and r.json() == {"ok": True}
    assert v2.seen["feedback"] == [FEEDBACK_BODY]


@pytest.mark.parametrize("path", ["/api/watches", "/api/feedback"])
def test_watch_and_feedback_bodies_must_be_json_objects(v2, path):
    assert v2.client.post(path, content=b"not json", headers=GOOD).json()["error"] == "bad_request"
    assert v2.client.post(path, json=[1, 2], headers=GOOD).status_code == 400
    assert v2.seen.get("watch_posts") is None and v2.seen.get("feedback") is None


def test_watch_and_feedback_writes_use_the_ask_rate_limit_not_the_daily_limit(v2, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    writes = [("/api/watches", WATCH_BODY), ("/api/feedback", FEEDBACK_BODY),
              ("/api/watches/w_0123456789ab/pause", None)]
    for n in range(30):
        path, body = writes[n % 3]
        assert v2.client.post(path, json=body, headers=GOOD).status_code < 400
    r = v2.client.post("/api/feedback", json=FEEDBACK_BODY, headers=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert auth.daily_questions.counts == {}
    auth.ask_limiter.hits.clear()
    assert v2.client.post("/api/ask", json={"question": "abc def"}, headers=GOOD).status_code == 202


def test_watch_reads_do_not_use_the_ask_rate_limit(v2):
    for _ in range(35):
        assert v2.client.get("/api/watches", headers=GOOD).status_code == 200


def test_watch_writes_502_when_agent_unreachable(v2, monkeypatch):
    remote_agent(monkeypatch, httpx.MockTransport(refused))
    for path, body in (("/api/watches", WATCH_BODY), ("/api/feedback", FEEDBACK_BODY)):
        r = v2.client.post(path, json=body, headers=GOOD)
        assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"


def test_alerts_route_reads_watches_from_the_agent(v2):
    item = rising_id()
    v2.seen["watches"] = [
        {"watch_id": "w_1", "target": {"kind": "item", "item_id": item}, "market": "ZA",
         "rule": {"state_in": ["rising"]}, "label": "Rising topic", "status": "active"},
        {"watch_id": "w_2", "target": {"kind": "item", "item_id": item}, "market": "ZA",
         "rule": {"creator_surge": True}, "label": "Surge", "status": "active"}]
    r = v2.client.get("/api/alerts", headers=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-09-30"
    (alert,) = body["alerts"]
    assert alert["watch_id"] == "w_1" and alert["fired_because"] == "Entered Rising"
    assert alert["card"]["item_id"] == item and alert["card"]["watch_id"] == "w_1"
    assert body["waiting"] == [{"watch_id": "w_2", "label": "Surge", "waiting": "Waiting for creator views detection"}]
    r = v2.client.get("/api/alerts", params={"date": "2026-09-29"}, headers=GOOD)
    assert r.status_code == 200 and r.json()["date"] == "2026-09-29"


def test_alerts_route_with_no_watches_is_an_empty_list(v2):
    r = v2.client.get("/api/alerts", headers=GOOD)
    assert r.status_code == 200
    assert r.json()["alerts"] == [] and r.json()["waiting"] == []


def test_alerts_route_errors(v2, monkeypatch, tmp_path):
    assert v2.client.get("/api/alerts", params={"date": "30-09-2026"}, headers=GOOD).status_code == 400
    v2.seen["watches_status"] = 500
    r = v2.client.get("/api/alerts", headers=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"
    v2.seen.pop("watches_status")
    from core.api.store import FixtureStore

    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: FixtureStore(root=tmp_path)), raising=False
    )
    r = v2.client.get("/api/alerts", headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_alerts_end_to_end_through_the_real_agent_in_process(v2, monkeypatch):
    import importlib

    agent_app = importlib.import_module("core.api.agent_app")  # ctx put a fake on the package attribute
    monkeypatch.delenv("F42_DATA", raising=False)
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)
    monkeypatch.setattr(agent_app, "WATCHES", [])
    body = {"target": {"kind": "item", "item_id": rising_id()}, "market": "ZA", "rule": {"state_in": ["rising"]}}
    r = v2.client.post("/api/watches", json=body, headers=GOOD)
    assert r.status_code == 201
    watch_id = r.json()["watch_id"]
    alerts = v2.client.get("/api/alerts", headers=GOOD).json()["alerts"]
    assert [a["watch_id"] for a in alerts] == [watch_id]
    assert v2.client.post(f"/api/watches/{watch_id}/pause", headers=GOOD).json()["status"] == "paused"
    assert v2.client.get("/api/alerts", headers=GOOD).json()["alerts"] == []


# Compare (contract section 11), over the real fixtures.

HERITAGE_ID = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"


def test_compare_route_needs_the_passcode_and_answers_each_mode(v2):
    items = {"mode": "items", "items": f"{STEP_ID},{HERITAGE_ID}", "market": "ZA", "days": 7}
    assert v2.client.get("/api/compare", params=items).status_code == 401
    r = v2.client.get("/api/compare", params=items, headers=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == {"from": "2026-09-24", "to": "2026-09-30", "days": 7}
    assert [s["key"] for s in body["subjects"]] == ["s1", "s2"] and body["notes"]
    r = v2.client.get("/api/compare", params={"mode": "markets", "items": STEP_ID, "markets": "ZA,NG,KE"},
                      headers=GOOD)
    assert r.status_code == 200 and [s["market"] for s in r.json()["subjects"]] == ["ZA", "NG", "KE"]
    r = v2.client.get("/api/compare", params={"mode": "platforms", "items": STEP_ID, "market": "ZA",
                                              "platforms": "tiktok,x"}, headers=GOOD)
    assert r.status_code == 200 and [s["platform"] for s in r.json()["subjects"]] == ["tiktok", "x"]


@pytest.mark.parametrize(
    "params",
    [{}, {"mode": "items", "items": STEP_ID, "market": "ZA"},
     {"mode": "items", "items": f"{STEP_ID},unknown_item", "market": "ZA"},
     {"mode": "items", "items": f"{STEP_ID},{HERITAGE_ID}", "market": "ZA", "days": "many"},
     {"mode": "items", "items": f"{STEP_ID},{HERITAGE_ID}", "market": "ZA", "days": 30},
     {"mode": "markets", "items": STEP_ID, "markets": "ZA"}],
)
def test_compare_route_400(v2, params):
    r = v2.client.get("/api/compare", params=params, headers=GOOD)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request" and r.json()["message"]


def test_compare_route_409_before_the_first_aggregate_run(v2, monkeypatch, tmp_path):
    from core.api.store import FixtureStore

    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: FixtureStore(root=tmp_path)), raising=False
    )
    r = v2.client.get("/api/compare", params={"mode": "items", "items": f"{STEP_ID},{HERITAGE_ID}", "market": "ZA"},
                      headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


# Version 4 (contract section 12): creators, communities and history, over the real fixtures.

NG_MACRO = "c_ng_macro"
V4_PATHS = [f"/api/creators/{NG_MACRO}?market=NG", "/api/communities?market=NG",
            f"/api/history/items/{HERITAGE_ID}?market=ZA", "/api/history/search?q=heritage", "/api/history/asks",
            "/api/history/briefs", "/api/history/findings"]


@pytest.mark.parametrize("path", V4_PATHS)
def test_v4_routes_need_the_passcode(v2, path):
    assert v2.client.get(path).status_code == 401
    assert v2.client.get(path, headers=GOOD).status_code == 200


def test_creator_route(v2):
    r = v2.client.get(f"/api/creators/{NG_MACRO}", params={"market": "ng"}, headers=GOOD)
    assert r.status_code == 200 and r.json()["creator"]["creator_id"] == NG_MACRO
    r = v2.client.get("/api/creators/c_ng_mid", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 404
    assert r.json() == {"error": "not_found", "message": "42 shows creators of this size only in totals"}
    r = v2.client.get("/api/creators/c_nobody", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 404 and r.json()["message"] == "No creator with that id"
    for params in ({}, {"market": "all"}):
        assert v2.client.get(f"/api/creators/{NG_MACRO}", params=params, headers=GOOD).status_code == 400
    assert v2.client.get("/api/creators/bad%20id!", params={"market": "NG"}, headers=GOOD).status_code == 400


def test_communities_routes(v2):
    r = v2.client.get("/api/communities", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 200
    listed = r.json()["communities"]
    assert len(listed) == 1
    cid = listed[0]["community_id"]
    r = v2.client.get(f"/api/communities/{cid}", headers=GOOD)
    assert r.status_code == 200 and r.json()["community"]["community_id"] == cid
    assert v2.client.get(f"/api/communities/{cid}", params={"market": "ZA"}, headers=GOOD).status_code == 404
    assert v2.client.get(f"/api/communities/{'0' * 64}", headers=GOOD).status_code == 404
    assert v2.client.get("/api/communities/bad%20id!", headers=GOOD).status_code == 400
    assert v2.client.get("/api/communities", headers=GOOD).status_code == 400


def test_history_routes(v2):
    r = v2.client.get(f"/api/history/items/{HERITAGE_ID}", params={"market": "ZA"}, headers=GOOD)
    assert r.status_code == 200 and r.json()["waves"]
    assert v2.client.get(f"/api/history/items/{'0' * 64}", params={"market": "ZA"}, headers=GOOD).status_code == 404
    assert v2.client.get(f"/api/history/items/{HERITAGE_ID}", headers=GOOD).status_code == 400
    r = v2.client.get("/api/history/search", params={"q": "heritage", "market": "ZA"}, headers=GOOD)
    assert r.status_code == 200 and [i["item_id"] for i in r.json()["items"]] == [HERITAGE_ID]
    assert v2.client.get("/api/history/search", params={"q": "h"}, headers=GOOD).status_code == 400
    r = v2.client.get("/api/history/asks", params={"limit": 1}, headers=GOOD)
    assert r.status_code == 200 and len(r.json()["asks"]) == 1 and r.json()["next_before"]
    assert v2.client.get("/api/history/asks", params={"limit": "many"}, headers=GOOD).status_code == 400
    r = v2.client.get("/api/history/briefs", params={"before": "2026-09-30"}, headers=GOOD)
    assert r.status_code == 200 and [d["date"] for d in r.json()["dates"]] == ["2026-09-29"]
    assert v2.client.get("/api/history/briefs", params={"before": "soon"}, headers=GOOD).status_code == 400
    r = v2.client.get("/api/history/findings", params={"item_id": STEP_ID}, headers=GOOD)
    assert r.status_code == 200 and [f["finding_id"] for f in r.json()["findings"]] == ["f_step_1"]


def test_history_item_route_409_before_the_first_detect_run(v2, monkeypatch, tmp_path):
    from core.api.store import FixtureStore

    monkeypatch.setattr(
        core.api, "store", types.SimpleNamespace(get_store=lambda: FixtureStore(root=tmp_path)), raising=False
    )
    r = v2.client.get(f"/api/history/items/{HERITAGE_ID}", params={"market": "ZA"}, headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_no_v4_route_returns_coord_score(v2):
    for path in V4_PATHS:
        assert "coord_score" not in v2.client.get(path, headers=GOOD).text


def test_a_suppressed_creator_in_card_evidence_never_reaches_creator_or_community_routes(v2, monkeypatch):
    from core.api.tests.test_people import InEvidence

    monkeypatch.setattr(core.api, "store", types.SimpleNamespace(get_store=InEvidence), raising=False)
    listed = v2.client.get("/api/communities", params={"market": "NG"}, headers=GOOD)
    cid = listed.json()["communities"][0]["community_id"]
    bodies = [listed, v2.client.get(f"/api/communities/{cid}", headers=GOOD),
              v2.client.get(f"/api/creators/{NG_MACRO}", params={"market": "NG"}, headers=GOOD)]
    for r in bodies:
        assert r.status_code == 200
        assert "ev_hidden" not in r.text and "fixture_ng_hidden" not in r.text.lower()
    assert "ev_macro" in bodies[2].text


# Fail closed (L1, 29 September): when the suppression view is missing or cannot be read, the people routes name
# no one. They answer 503 people_unavailable with plain words, and no creator name or handle reaches the reader.
PEOPLE_PATHS = [f"/api/creators/{NG_MACRO}?market=NG", "/api/communities?market=NG"]


def _suppression_fails(monkeypatch, how):
    from core.api.store import FixtureStore

    class Store(FixtureStore):
        def suppressed_creators(self):
            if how == "missing":
                return None
            raise RuntimeError("Access Denied: v_suppressed_creators")

    monkeypatch.setattr(core.api, "store", types.SimpleNamespace(get_store=lambda: Store()), raising=False)


@pytest.mark.parametrize("how", ["missing", "unreadable"])
@pytest.mark.parametrize("path", PEOPLE_PATHS)
def test_people_routes_fail_closed_without_the_suppression_list(v2, monkeypatch, how, path):
    _suppression_fails(monkeypatch, how)
    r = v2.client.get(path, headers=GOOD)
    assert r.status_code == 503
    assert r.json() == {"error": "people_unavailable", "message": "People view unavailable."}
    assert "fixture_" not in r.text and "@" not in r.text and "Access Denied" not in r.text


def test_community_route_fails_closed_without_the_suppression_list(v2, monkeypatch):
    communities = v2.client.get("/api/communities?market=NG", headers=GOOD).json()["communities"]
    assert communities, "the fixtures hold at least one community"
    path = f"/api/communities/{communities[0]['community_id']}?market=NG"
    assert v2.client.get(path, headers=GOOD).status_code == 200
    _suppression_fails(monkeypatch, "unreadable")
    r = v2.client.get(path, headers=GOOD)
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"


@pytest.mark.parametrize("status,payload,expected_agent,expected_ready", [
    (200, {"t2_ready": True}, "ok", True),
    (200, {"t2_ready": False}, "ok", False),
    (200, {"t2_ready": "true"}, "ok", False),
    (503, {"t2_ready": True}, "status 503", False),
    (200, "invalid_json", "ok", False),
    (200, [], "ok", False),
    (None, None, "unreachable", False),
])
def test_health_uses_one_fresh_agent_response_for_status_and_readiness(ctx, monkeypatch, status, payload,
                                                                    expected_agent, expected_ready):
    calls = []

    def answer(request):
        calls.append(request.url.path)
        if status is None:
            raise httpx.ConnectError("fixture connection failure", request=request)
        if payload == "invalid_json":
            return httpx.Response(status, content=b"not json")
        return httpx.Response(status, json=payload)

    remote_agent(monkeypatch, httpx.MockTransport(answer))
    response = ctx.client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["checks"]["agent"] == expected_agent
    assert response.json()["t2_ready"] is expected_ready
    assert calls == ["/health"]
    monkeypatch.setenv("F42_AUTH_MODE", "fixture_unavailable")
    api_mod._reset_health_cache()  # the cache window has passed
    second = ctx.client.get("/api/health").json()
    assert calls == ["/health", "/health"]
    assert second["auth_mode"] == "unavailable" and second["passcode"] is False
    assert second["checks"]["auth"] == "not_configured" and second["ok"] is False


# F2a: the open health route must not run its three BigQuery-backed checks on every request.

def test_health_runs_its_checks_once_inside_the_cache_window_and_again_after_it(ctx, monkeypatch):
    runs = {"bigquery": 0, "agent": 0, "today": 0}
    clock = {"t": 1000.0}

    def bigquery():
        runs["bigquery"] += 1
        return "ok"

    def today():
        runs["today"] += 1
        return "ok"

    async def agent():
        runs["agent"] += 1
        return "ok", True

    monkeypatch.setattr(api_mod, "_bigquery_check", bigquery)
    monkeypatch.setattr(api_mod, "_today_check", today)
    monkeypatch.setattr(api_mod, "_agent_check", agent)
    monkeypatch.setattr(api_mod, "_health_clock", lambda: clock["t"], raising=False)
    api_mod._reset_health_cache()

    bodies = [ctx.client.get("/api/health") for _ in range(20)]
    assert {r.status_code for r in bodies} == {200}
    assert runs == {"bigquery": 1, "agent": 1, "today": 1}
    assert bodies[0].json()["checks"] == {"auth": "ok", "bigquery": "ok", "agent": "ok", "today": "ok"}

    clock["t"] += api_mod.HEALTH_CACHE_SECONDS + 1
    ctx.client.get("/api/health")
    assert runs == {"bigquery": 2, "agent": 2, "today": 2}


def test_health_cache_keeps_time_and_auth_fresh(ctx, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(api_mod, "_health_clock", lambda: clock["t"], raising=False)
    api_mod._reset_health_cache()
    first = ctx.client.get("/api/health").json()
    ctx.client.app  # same app
    monkeypatch.setenv("F42_VERSION", "def456")
    second = ctx.client.get("/api/health").json()
    assert first["version"] == "abc123" and second["version"] == "def456"


# F2b: Radar was 2.1 MB and Discover 308 KB, both sent uncompressed.

@pytest.fixture
def big_routes():
    from fastapi.responses import PlainTextResponse

    @api_mod.app.get("/__big_json")
    def big_json():
        return {"rows": [{"hash": "sha256:" + "ab" * 32, "n": n} for n in range(400)]}

    @api_mod.app.get("/__big_stream")
    def big_stream():
        return PlainTextResponse("data: " + "x" * 5000 + "\n\n", media_type="text/event-stream")

    paths = {"/__big_json", "/__big_stream"}
    routes = api_mod.app.router.routes
    added = [r for r in routes if getattr(r, "path", None) in paths]
    routes[:] = added + [r for r in routes if r not in added]  # ahead of the app-shell catch-all
    yield
    api_mod.app.router.routes[:] = [r for r in api_mod.app.router.routes if getattr(r, "path", None) not in paths]


def test_large_json_is_gzipped_for_a_client_that_accepts_it(ctx, big_routes):
    plain = ctx.client.get("/__big_json", headers={"Accept-Encoding": "identity"})
    zipped = ctx.client.get("/__big_json", headers={"Accept-Encoding": "gzip"})
    assert plain.headers.get("content-encoding") is None
    assert zipped.headers.get("content-encoding") == "gzip"
    assert zipped.json() == plain.json()
    assert int(zipped.headers["content-length"]) < len(plain.content) / 3


def test_an_event_stream_is_never_gzipped(ctx, big_routes):
    r = ctx.client.get("/__big_stream", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") is None


# Contract 3.4b (C2 v2.1 section 2.1, tests AU-01 to AU-09): the request URL and the token audience are two values.
# The audience is always the canonical agent URL; a tag URL is only ever a place to send requests.
CANON = "https://f42-agent-fibxg5ynpq-uc.a.run.app"
TAGGED = "https://rel-1234567-01---f42-agent-fibxg5ynpq-uc.a.run.app"


def agent_config(monkeypatch, url, audience=None):
    monkeypatch.setenv("AGENT_URL", url)
    if audience is None:
        monkeypatch.delenv("AGENT_AUDIENCE", raising=False)
    else:
        monkeypatch.setenv("AGENT_AUDIENCE", audience)
    minted = []
    monkeypatch.setattr(api_mod, "_id_token", lambda audience: minted.append(audience) or "idtok")
    return minted


def build_client():
    import asyncio
    return asyncio.run(api_mod._agent_client(httpx.Timeout(1.0)))


def refusal_of(monkeypatch, url, audience, caplog):
    from core.api.auth import ApiError
    minted = agent_config(monkeypatch, url, audience)
    with caplog.at_level("WARNING", logger="f42.api"):
        with pytest.raises(ApiError) as caught:
            build_client()
    assert (caught.value.status, caught.value.error) == (502, "agent_unavailable")
    assert minted == []  # no token is minted for a refused configuration
    assert url not in caplog.text and (not audience or audience not in caplog.text)


def test_au01_a_canonical_agent_url_with_no_audience_is_its_own_audience(monkeypatch):
    minted = agent_config(monkeypatch, CANON)
    assert str(build_client().base_url).rstrip("/") == CANON
    assert minted == [CANON]


def test_au02_a_tagged_request_url_still_mints_the_token_for_the_canonical_audience(monkeypatch):
    minted = agent_config(monkeypatch, TAGGED, CANON)
    client = build_client()
    assert str(client.base_url).rstrip("/") == TAGGED
    assert minted == [CANON]
    assert client.headers["Authorization"] == "Bearer idtok"


def test_au03_a_tagged_request_url_with_no_audience_fails_closed(monkeypatch, caplog):
    refusal_of(monkeypatch, TAGGED, None, caplog)


@pytest.mark.parametrize("audience", [CANON + "/x", CANON + "/", CANON.upper(), "http://f42-agent-fibxg5ynpq-uc.a.run.app",
                                      CANON + "?a=1", "f42-agent-fibxg5ynpq-uc.a.run.app"])
def test_au04_a_malformed_audience_is_refused(monkeypatch, caplog, audience):
    refusal_of(monkeypatch, TAGGED, audience, caplog)
    caplog.clear()
    refusal_of(monkeypatch, CANON, audience, caplog)


def test_au05_a_canonical_url_with_an_audience_on_another_host_is_refused(monkeypatch, caplog):
    refusal_of(monkeypatch, CANON, "https://f42-other-fibxg5ynpq-uc.a.run.app", caplog)


def test_a_canonical_url_with_the_same_audience_is_accepted(monkeypatch):
    minted = agent_config(monkeypatch, CANON, CANON)
    build_client()
    assert minted == [CANON]


def test_au06_a_local_agent_url_mints_no_token_whatever_the_audience(monkeypatch):
    minted = agent_config(monkeypatch, "http://127.0.0.1:8081", "not even a url")
    assert str(build_client().base_url).rstrip("/") == "http://127.0.0.1:8081"
    assert minted == []


def test_no_agent_url_stays_the_in_process_agent(monkeypatch):
    minted = agent_config(monkeypatch, "", "garbage")
    assert str(build_client().base_url).startswith("http://f42-agent")
    assert minted == []


def test_au07_the_health_check_and_the_events_relay_use_the_same_audience_rule(ctx, monkeypatch):
    seen = []
    agent_config(monkeypatch, TAGGED, CANON)
    monkeypatch.setattr(api_mod, "_id_token", lambda audience: seen.append(audience) or "idtok")

    def handler(request):
        seen.append(str(request.url.host))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "t2_ready": True})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    import asyncio
    assert asyncio.run(api_mod._agent_check()) == ("ok", True)
    ctx.client.get("/api/ask/a_1/events", headers=GOOD)
    assert CANON in seen and TAGGED not in seen
    assert {s for s in seen if s.startswith("https://")} == {CANON}
    assert "rel-1234567-01---f42-agent-fibxg5ynpq-uc.a.run.app" in seen

    from core.api.auth import ApiError
    agent_config(monkeypatch, TAGGED, None)
    assert asyncio.run(api_mod._agent_check()) == ("unreachable", False)
    with pytest.raises(ApiError):
        build_client()


@pytest.mark.parametrize("url", ["https://rel-1234567-01---f42-other-fibxg5ynpq-uc.a.run.app",
                                 "https://---f42-agent-fibxg5ynpq-uc.a.run.app",
                                 "https://rel-1234567-01---rel-1234567-01---f42-agent-fibxg5ynpq-uc.a.run.app"])
def test_au08_a_tag_host_that_does_not_end_in_the_audience_host_is_refused(monkeypatch, caplog, url):
    refusal_of(monkeypatch, url, CANON, caplog)


def test_au09_the_tag_url_the_release_tools_build_is_accepted(monkeypatch):
    from ops.deploy.release import asset_origin
    tagged = asset_origin(CANON, "question-abc1234")
    minted = agent_config(monkeypatch, tagged, CANON)
    assert str(build_client().base_url).rstrip("/") == tagged
    assert minted == [CANON]


def test_a_tag_host_is_never_the_audience(monkeypatch, caplog):
    """C2 2.1: the audience is the service itself, never a revision tag URL."""
    from core.api.auth import ApiError
    tagged = "https://t---a---svc-abc-uc.a.run.app"
    minted = agent_config(monkeypatch, tagged, "https://a---svc-abc-uc.a.run.app")
    with caplog.at_level("WARNING", logger="f42.api"), pytest.raises(ApiError):
        build_client()
    assert minted == []


def test_discover_route_takes_several_platforms_and_states(v2):
    get = v2.client.get
    one = get("/api/discover", params={"market": "ZA", "platform": "instagram"}, headers=GOOD).json()
    assert [c["title"] for c in one["items"]] == ["#fixture_za_step"]
    comma = get("/api/discover", params={"market": "ZA", "platform": "x,instagram"}, headers=GOOD).json()
    repeated = get("/api/discover?market=ZA&platform=x&platform=instagram", headers=GOOD).json()
    assert [c["item_id"] for c in comma["items"]] == [c["item_id"] for c in repeated["items"]]
    assert len(comma["items"]) == 2
    states = get("/api/discover?market=ZA&state=rising&state=emerging", headers=GOOD).json()
    assert len(states["items"]) == 2
    radar = get("/api/discover/radar?market=ZA&platform=x&platform=instagram&state=rising,emerging", headers=GOOD).json()
    assert len(radar["points"]) == 2


@pytest.mark.parametrize("query", ["platform=myspace", "platform=x,nope", "state=viral", "platform=x&platform=%27%3Bdrop"])
def test_discover_routes_refuse_unknown_platforms_and_states(v2, query):
    for path in ("/api/discover?market=ZA&", "/api/discover/radar?market=ZA&"):
        r = v2.client.get(path + query, headers=GOOD)
        assert r.status_code == 400 and r.json()["error"] == "bad_request" and r.json()["message"]
