"""f42-api forwarding of /api/dossiers and /api/investigations to f42-agent (contract.md sections 2, 6, 13)."""

import copy
import importlib
import json
import types

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

import core.api
from core.api import app as api_mod
from core.api import auth

PASS = "s3cret-passcode"
GOOD = {"X-Passcode": PASS}
PDF = b"%PDF-1.7\n\x00\xff binary body"
SSE = ('id: {n}\nevent: step\ndata: {{"seq": {n}, "text": "after {lei}"}}\n\n'
       'id: 9\nevent: done\ndata: {{"seq": 9, "status": "complete", "url": "/api/investigations/i_1"}}\n\n')


def make_agent(seen):
    """An agent that echoes what it received, with a few ids that answer with errors."""
    agent = FastAPI()

    @agent.get("/health")
    async def health():
        return {"ok": True}

    @agent.get("/api/dossiers/{dossier_id}/versions/{version}/export")
    async def export(dossier_id: str, version: str, request: Request):
        seen.setdefault("calls", []).append(("GET", request.url.path, dict(request.query_params), None))
        fmt = request.query_params.get("format", "html")
        name = f'attachment; filename="42-dossier-{dossier_id}-v{version}.{fmt}"'
        if fmt == "pdf":
            return Response(PDF, media_type="application/pdf", headers={"Content-Disposition": name})
        return Response("<html>dossier</html>", media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": name})

    @agent.get("/api/investigations/{investigation_id}/events")
    async def events(investigation_id: str, request: Request):
        seen.setdefault("calls", []).append(("GET", request.url.path, {}, None))
        if investigation_id == "i_missing":
            return JSONResponse({"error": "not_found", "message": "No investigation i_missing is stored."},
                                status_code=404)
        if investigation_id == "i_draft":
            return JSONResponse({"error": "not_ready", "message": "Investigation i_draft has not started."},
                                status_code=409)
        lei = request.headers.get("last-event-id", "none")

        async def gen():
            yield SSE.format(n=4, lei=lei)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @agent.api_route("/api/{path:path}", methods=["GET", "POST", "PUT"])
    async def echo(path: str, request: Request):
        raw = await request.body()
        body = json.loads(raw) if raw else None
        seen.setdefault("calls", []).append((request.method, request.url.path, dict(request.query_params), body))
        if "d_missing" in path or "i_missing" in path:
            return JSONResponse({"error": "not_found", "message": "not stored"}, status_code=404)
        if path.endswith("/start") and "i_draft" not in path:
            return JSONResponse({"error": "not_ready", "message": "not a draft"}, status_code=409)
        status = 201 if request.method == "POST" and not path.endswith("/stop") else 200
        if path.endswith("/start") or path.endswith("/stop") or path == "ask":
            status = 202
        return JSONResponse({"echo": [request.method, "/api/" + path, dict(request.query_params), body]},
                            status_code=status)

    return agent


@pytest.fixture
def ctx(monkeypatch):
    seen = {}
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("ASK_PER_IP_DAILY", raising=False)
    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=make_agent(seen)), raising=False)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()
    yield types.SimpleNamespace(client=TestClient(api_mod.app), seen=seen)
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()


TICK = {"claim_id": "c1", "ticked": True, "note": "Checked the post"}
EDIT = {"keep": ["c1"], "order": ["c1"], "title": "Pot dance", "notes": {"c1": "Good"}}
PLAN = {"plan": {"sub_questions": [], "researchers": 5}}
DRAFT = {"question": "What is behind #fixture in South Africa?", "market": "ZA"}

# (method, f42-api path, body, agent path, query the agent sees, status)
ROUTES = [
    ("POST", "/api/dossiers", {"from": {"ask_id": "a_1"}, "title": "T"}, "/api/dossiers", {}, 201),
    ("GET", "/api/dossiers?limit=5&before=2026-09-29T10:00:00%2B02:00", None, "/api/dossiers",
     {"limit": "5", "before": "2026-09-29T10:00:00+02:00"}, 200),
    ("GET", "/api/dossiers/d_1", None, "/api/dossiers/d_1", {}, 200),
    ("PUT", "/api/dossiers/d_1", EDIT, "/api/dossiers/d_1", {}, 200),
    ("POST", "/api/dossiers/d_1/ticks", TICK, "/api/dossiers/d_1/ticks", {}, 201),
    ("POST", "/api/dossiers/d_1/freeze", None, "/api/dossiers/d_1/freeze", {}, 201),
    ("GET", "/api/dossiers/d_1/versions/2", None, "/api/dossiers/d_1/versions/2", {}, 200),
    ("POST", "/api/investigations", DRAFT, "/api/investigations", {}, 201),
    ("GET", "/api/investigations?status=running", None, "/api/investigations", {"status": "running"}, 200),
    ("GET", "/api/investigations/i_1", None, "/api/investigations/i_1", {}, 200),
    ("PUT", "/api/investigations/i_1/plan", PLAN, "/api/investigations/i_1/plan", {}, 200),
    ("POST", "/api/investigations/i_draft/start", None, "/api/investigations/i_draft/start", {}, 202),
    ("POST", "/api/investigations/i_1/stop", None, "/api/investigations/i_1/stop", {}, 202),
]
EVERY_PATH = [(m, p.split("?")[0]) for m, p, *_ in ROUTES] + [
    ("GET", "/api/dossiers/d_1/versions/2/export"), ("GET", "/api/investigations/i_1/events")]
WRITES = [r for r in ROUTES if r[0] != "GET"]
BAD_IDS = ["bad%20id!", "a%3Bdrop", "x" * 129]


@pytest.mark.parametrize("method, path", EVERY_PATH)
def test_every_route_needs_the_passcode(ctx, method, path):
    r = ctx.client.request(method, path, json={})
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert "calls" not in ctx.seen


@pytest.mark.parametrize("method, path, body, agent_path, query, status", ROUTES)
def test_every_route_forwards_method_path_query_and_body(ctx, method, path, body, agent_path, query, status):
    r = ctx.client.request(method, path, json=body, headers=GOOD)
    assert r.status_code == status
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"echo": [method, agent_path, query, body]}
    assert ctx.seen["calls"] == [(method, agent_path, query, body)]


def test_agent_errors_pass_through_with_their_status_and_words(ctx):
    r = ctx.client.get("/api/dossiers/d_missing", headers=GOOD)
    assert r.status_code == 404 and r.json() == {"error": "not_found", "message": "not stored"}
    r = ctx.client.post("/api/investigations/i_1/start", headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_list_forwards_only_the_params_it_knows(ctx):
    r = ctx.client.get("/api/dossiers?limit=3&evil=1", headers=GOOD)
    assert r.json()["echo"][2] == {"limit": "3"}
    r = ctx.client.get("/api/dossiers", headers=GOOD)
    assert r.json()["echo"][2] == {}
    r = ctx.client.get("/api/investigations?status=draft&x=y", headers=GOOD)
    assert r.json()["echo"][2] == {"status": "draft"}


@pytest.mark.parametrize("method, template", [
    ("GET", "/api/dossiers/{}"), ("PUT", "/api/dossiers/{}"), ("POST", "/api/dossiers/{}/ticks"),
    ("POST", "/api/dossiers/{}/freeze"), ("GET", "/api/dossiers/{}/versions/1"),
    ("GET", "/api/dossiers/{}/versions/1/export"), ("GET", "/api/investigations/{}"),
    ("PUT", "/api/investigations/{}/plan"), ("POST", "/api/investigations/{}/start"),
    ("GET", "/api/investigations/{}/events"), ("POST", "/api/investigations/{}/stop"),
])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_ids_are_checked_like_ask_ids_before_forwarding(ctx, method, template, bad):
    r = ctx.client.request(method, template.format(bad), json={}, headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen


def test_version_must_be_a_whole_number(ctx):
    for path in ("/api/dossiers/d_1/versions/two", "/api/dossiers/d_1/versions/two/export"):
        r = ctx.client.get(path, headers=GOOD)
        assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/dossiers"), ("PUT", "/api/dossiers/d_1"), ("POST", "/api/dossiers/d_1/ticks"),
    ("POST", "/api/dossiers/d_1/freeze"),
    ("POST", "/api/investigations"), ("PUT", "/api/investigations/i_1/plan"),
])
def test_bodies_must_be_json_objects(ctx, method, path):
    r = ctx.client.request(method, path, content=b"not json", headers={**GOOD, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = ctx.client.request(method, path, json=[1, 2], headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen


def test_freeze_forwards_from_version_and_an_empty_body_stays_empty(ctx):
    r = ctx.client.post("/api/dossiers/d_1/freeze", json={"from_version": 3}, headers=GOOD)
    assert r.status_code == 201
    r = ctx.client.post("/api/dossiers/d_1/freeze", content=b"", headers=GOOD)
    assert r.status_code == 201
    assert ctx.seen["calls"] == [("POST", "/api/dossiers/d_1/freeze", {}, {"from_version": 3}),
                                 ("POST", "/api/dossiers/d_1/freeze", {}, None)]


@pytest.mark.parametrize("fmt, kind", [("pdf", "application/pdf"), ("html", "text/html")])
def test_export_passes_bytes_type_and_filename_through(ctx, fmt, kind):
    r = ctx.client.get("/api/dossiers/d_1/versions/2/export", params={"format": fmt}, headers=GOOD)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(kind)
    assert r.headers["content-disposition"] == f'attachment; filename="42-dossier-d_1-v2.{fmt}"'
    if fmt == "pdf":
        assert r.content == PDF
    assert ctx.seen["calls"] == [("GET", "/api/dossiers/d_1/versions/2/export", {"format": fmt}, None)]


def test_export_defaults_to_html(ctx):
    ctx.client.get("/api/dossiers/d_1/versions/2/export", headers=GOOD)
    assert ctx.seen["calls"][0][2] == {"format": "html"}


def test_investigation_events_relay_the_stream_with_last_event_id(ctx):
    r = ctx.client.get("/api/investigations/i_1/events", headers={**GOOD, "Last-Event-ID": "3"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == SSE.format(n=4, lei="3")


def test_investigation_events_pass_agent_refusals_through(ctx):
    r = ctx.client.get("/api/investigations/i_missing/events", headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = ctx.client.get("/api/investigations/i_draft/events", headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_writes_use_the_ask_rate_limit_and_reads_do_not(ctx):
    for n in range(30):
        method, path, body, *_ = WRITES[n % len(WRITES)]
        if path.endswith("/start"):
            path = "/api/investigations/i_draft/stop"
        assert ctx.client.request(method, path, json=body, headers=GOOD).status_code < 400
    for method, path, body, *_ in WRITES:
        r = ctx.client.request(method, path, json=body, headers=GOOD)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    for _ in range(5):
        assert ctx.client.get("/api/dossiers/d_1", headers=GOOD).status_code == 200
        assert ctx.client.get("/api/investigations", headers=GOOD).status_code == 200
        assert ctx.client.get("/api/dossiers/d_1/versions/2/export", headers=GOOD).status_code == 200
    assert auth.daily_questions.counts == {}


def test_only_investigation_start_counts_as_a_live_question(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    for method, path, body, *_ in WRITES:
        if not path.endswith("/start"):
            assert ctx.client.request(method, path, json=body, headers=GOOD).status_code < 400
    assert auth.daily_questions.counts == {}
    assert ctx.client.post("/api/investigations/i_draft/start", headers=GOOD).status_code == 202
    r = ctx.client.post("/api/investigations/i_draft/start", headers=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "daily_question_limit"
    r = ctx.client.post("/api/ask", json={"question": "What is behind #fixture?"}, headers=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "daily_question_limit"
    starts = [c for c in ctx.seen["calls"] if c[1].endswith("/start")]
    assert len(starts) == 1


def test_a_start_the_agent_refuses_does_not_count(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    assert ctx.client.post("/api/investigations/i_1/start", headers=GOOD).status_code == 409
    assert ctx.client.post("/api/investigations/i_draft/start", headers=GOOD).status_code == 202


def test_502_when_the_agent_cannot_be_reached_and_start_does_not_count(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    monkeypatch.setenv("AGENT_URL", "https://f42-agent.example.run.app")
    monkeypatch.setattr(api_mod, "_id_token", lambda audience: "idtok")
    real = httpx.AsyncClient

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(refused)}))
    for method, path in (("GET", "/api/dossiers"), ("POST", "/api/investigations/i_draft/start"),
                         ("GET", "/api/dossiers/d_1/versions/2/export"), ("GET", "/api/investigations/i_1/events")):
        r = ctx.client.request(method, path, headers=GOOD)
        assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"
    assert all(used == 0 for _, used in auth.daily_questions.counts.values())


# The whole flow through f42-api to the real f42-agent in process, on the fixture agent and planner.


@pytest.fixture
def real(ctx, monkeypatch):
    agent_app = importlib.import_module("core.api.agent_app")  # ctx put a fake on the package attribute
    from core.api import investigations, pdf
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.001")
    for name in ("F42_DATA", "ASK_DAILY", "MODEL_DAILY_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_app, "_runs_have_model_usd", False, raising=False)
    monkeypatch.setattr(pdf, "render_pdf", lambda html: b"%PDF-1.4 " + html[:20].encode())
    held = (agent_app.SINK, agent_app.ASKS, agent_app.INVESTIGATIONS, agent_app.RUNNING_INVESTIGATIONS,
            agent_app.DOSSIER_VERSIONS, agent_app.DOSSIER_REVIEWS)

    def clear():
        for asks in list(agent_app.ASKS.values()):
            asks.stop = True
            asks.finished.wait(5)
        for h in held:
            h.clear()
        investigations.RESERVED.clear()

    clear()
    yield ctx
    clear()


def freeze_and_export(client, dossier):
    dossier_id = dossier["dossier_id"]
    for need in dossier["needs_tick"]:
        r = client.post(f"/api/dossiers/{dossier_id}/ticks", json={"claim_id": need["claim_id"], "ticked": True},
                        headers=GOOD)
        assert r.status_code == 201
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", headers=GOOD)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    html = client.get(f"/api/dossiers/{dossier_id}/versions/{version}/export", params={"format": "html"},
                      headers=GOOD)
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert html.headers["content-disposition"] == f'attachment; filename="42-dossier-{dossier_id}-v{version}.html"'
    pdf = client.get(f"/api/dossiers/{dossier_id}/versions/{version}/export", params={"format": "pdf"},
                     headers=GOOD)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    return version


def test_dossier_and_investigation_flow_through_the_real_agent(real):
    client = real.client
    record = client.post("/api/ask", json={"question": "What is behind #fixture in South Africa this week?",
                                           "market": "ZA", "wait": True}, headers=GOOD).json()
    r = client.post("/api/dossiers", json={"from": {"ask_id": record["ask_id"]}, "title": "Pot dance"}, headers=GOOD)
    assert r.status_code == 201, r.text
    dossier = r.json()
    assert dossier["source_ask_id"] == record["ask_id"] and dossier["title"] == "Pot dance"
    freeze_and_export(client, dossier)
    listed = client.get("/api/dossiers", params={"limit": 5}, headers=GOOD).json()["dossiers"]
    assert [d["dossier_id"] for d in listed] == [dossier["dossier_id"]]

    r = client.post("/api/investigations", json={"question": "What is behind #fixture in South Africa?",
                                                 "market": "ZA"}, headers=GOOD)
    assert r.status_code == 201, r.text
    inv_id, plan = r.json()["investigation_id"], r.json()["plan"]
    r = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}}, headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    r = client.put(f"/api/investigations/{inv_id}/plan", json={"plan": plan}, headers=GOOD)
    assert r.status_code == 200 and r.json()["version"] == 2
    started = client.post(f"/api/investigations/{inv_id}/start", headers=GOOD)
    assert started.status_code == 202, started.text
    stream = client.get(f"/api/investigations/{inv_id}/events", headers=GOOD)
    assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in stream.text and '"status": "complete"' in stream.text
    read = client.get(f"/api/investigations/{inv_id}", headers=GOOD).json()
    assert read["record"]["status"] == "complete"
    assert client.get("/api/investigations", params={"status": "complete"}, headers=GOOD).json()[
        "investigations"][0]["investigation_id"] == inv_id

    r = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}}, headers=GOOD)
    assert r.status_code == 201, r.text
    from_inv = r.json()
    assert from_inv["source_ask_id"] == started.json()["ask_id"]
    assert from_inv["source"] == {"investigation_id": inv_id}
    freeze_and_export(client, from_inv)


def test_a_stale_freeze_through_f42_api_gives_409_and_appends_nothing(real):
    client = real.client
    agent_app = importlib.import_module("core.api.agent_app")
    record = client.post("/api/ask", json={"question": "What is behind #fixture in South Africa this week?",
                                           "market": "ZA", "wait": True}, headers=GOOD).json()
    dossier = client.post("/api/dossiers", json={"from": {"ask_id": record["ask_id"]}}, headers=GOOD).json()
    dossier_id = dossier["dossier_id"]
    for need in dossier["needs_tick"]:
        client.post(f"/api/dossiers/{dossier_id}/ticks", json={"claim_id": need["claim_id"], "ticked": True},
                    headers=GOOD)
    # Another tab edits version 1 into version 2; this tab still shows version 1 and presses Freeze.
    r = client.put(f"/api/dossiers/{dossier_id}", json={"title": "Tab B", "from_version": 1}, headers=GOOD)
    assert r.status_code == 200 and r.json()["version"] == 2
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 1}, headers=GOOD)
    assert r.status_code == 409
    assert r.json() == {"error": "stale_version",
                        "message": "This dossier changed since you opened it. Reload to see the latest version."}
    assert [row["state"] for row in agent_app.DOSSIER_VERSIONS] == ["draft", "draft"]
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 2}, headers=GOOD)
    assert r.status_code == 201 and r.json()["version"] == 3 and r.json()["state"] == "frozen"


# Scheduled questions (contract section 14.2): forwarded to f42-agent like the watches.

SCHEDULE = {"question": "What is behind #fixture in South Africa?", "market": "ZA", "tier": "T1",
            "cadence": "daily", "deliver": ["in_app"]}
SCHEDULE_ROUTES = [
    ("GET", "/api/schedules", None, "/api/schedules", {}, 200),
    ("POST", "/api/schedules", SCHEDULE, "/api/schedules", {}, 201),
    ("POST", "/api/schedules/s_1/pause", None, "/api/schedules/s_1/pause", {}, 201),
    ("POST", "/api/schedules/s_1/resume", None, "/api/schedules/s_1/resume", {}, 201),
]


@pytest.mark.parametrize("method, path, body, agent_path, query, status", SCHEDULE_ROUTES)
def test_schedule_routes_forward_to_the_agent(ctx, method, path, body, agent_path, query, status):
    assert ctx.client.request(method, path, json=body).status_code == 401
    r = ctx.client.request(method, path, json=body, headers=GOOD)
    assert r.status_code == status
    assert r.json() == {"echo": [method, agent_path, query, body]}
    assert ctx.seen["calls"] == [(method, agent_path, query, body)]


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_schedule_ids_are_checked_before_forwarding(ctx, action, bad):
    r = ctx.client.post(f"/api/schedules/{bad}/{action}", headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen


def test_schedule_writes_use_the_ask_rate_limit_and_are_not_live_questions(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    writes = [r for r in SCHEDULE_ROUTES if r[0] == "POST"]
    for n in range(30):
        method, path, body, *_ = writes[n % len(writes)]
        assert ctx.client.request(method, path, json=body, headers=GOOD).status_code < 400
    for method, path, body, *_ in writes:
        r = ctx.client.request(method, path, json=body, headers=GOOD)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert ctx.client.get("/api/schedules", headers=GOOD).status_code == 200
    assert auth.daily_questions.counts == {}


# Spike explanation (contract section 14.1): a shorthand for a T1 ask about one measured day.

HERITAGE = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"  # #fixture_za_heritage, ZA


@pytest.fixture
def spikes(ctx, monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    return ctx


def spike(client, **body):
    return client.post("/api/spikes", json={"item_id": HERITAGE, "market": "ZA", "date": "2026-09-28", **body},
                       headers=GOOD)


def forwarded_asks(ctx):
    return [c for c in ctx.seen.get("calls", []) if c[1] == "/api/ask"]


def test_spike_forwards_a_t1_ask_with_the_built_question(spikes):
    r = spike(spikes.client, series="feed_tiktok")
    assert r.status_code == 202
    ((method, path, query, body),) = forwarded_asks(spikes)
    assert (method, query) == ("POST", {})
    assert body == {
        "question": "What made #fixture_za_heritage jump in South Africa on 2026-09-28?",
        "market": "ZA", "tier": "T1",
        "from_card": {"item_id": HERITAGE, "market": "ZA", "date": "2026-09-28"},
        "spike": {"item_id": HERITAGE, "market": "ZA", "date": "2026-09-28", "series": "feed_tiktok"}}
    assert r.json() == {"echo": ["POST", "/api/ask", {}, body]}


@pytest.mark.parametrize("series", ["feed_tiktok", None])
def test_the_built_spike_passes_the_agents_spike_check(spikes, series):
    from core.api.spike import check_spike

    assert spike(spikes.client, market="za", series=series).status_code == 202
    built = forwarded_asks(spikes)[0][3]["spike"]
    assert set(built) == {"item_id", "market", "date", "series"} and check_spike(built) == built


def test_spike_without_a_series_accepts_any_measured_series(spikes):
    r = spike(spikes.client)
    assert r.status_code == 202
    assert forwarded_asks(spikes)[0][3]["spike"]["series"] is None


@pytest.mark.parametrize("day, series", [
    ("2026-09-27", None),            # the TikTok feed has a gap and X has no row that day
    ("2026-09-27", "feed_tiktok"),
    ("2026-09-24", "panel_x_hub"),   # measured on TikTok, not on the named X series
    ("2026-08-01", None),            # outside the series
])
def test_spike_on_a_gap_gives_400_and_asks_nothing(spikes, day, series):
    r = spike(spikes.client, date=day, series=series)
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "message": "No measurement that day"}
    assert forwarded_asks(spikes) == []
    assert auth.daily_questions.counts == {}


def test_spike_on_a_measured_named_series_is_asked(spikes):
    assert spike(spikes.client, date="2026-09-28", series="panel_x_hub").status_code == 202


def test_spike_label_falls_back_to_the_item_id(spikes, monkeypatch):
    from core.api import store

    class Unknown(store.FixtureStore):
        def item_series(self, item_id, market, days):
            return [{"item_id": item_id, "market": market, "series": "feed_tiktok", "day": "2026-09-28",
                     "value": 4}]

    monkeypatch.setattr(store, "get_store", Unknown)
    r = spike(spikes.client, item_id="not_in_the_run")
    assert r.status_code == 202
    assert forwarded_asks(spikes)[0][3]["question"] == "What made not_in_the_run jump in South Africa on 2026-09-28?"


@pytest.mark.parametrize("body", [
    {"item_id": "bad id!"}, {"item_id": None}, {"item_id": "x" * 129}, {"item_id": 5},
    {"market": "GH"}, {"market": None}, {"market": "all"},
    {"date": "28 September"}, {"date": "2026-02-30"}, {"date": None},
    {"series": 5}, {"series": ""}, {"series": "x" * 129}, {"series": "x" * 81}, {"series": "feed-tiktok"},
])
def test_bad_spikes_give_400(spikes, body):
    r = spike(spikes.client, **body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert forwarded_asks(spikes) == []


@pytest.mark.parametrize("series", ["feed-tiktok", "x" * 81])
def test_a_measured_series_the_agent_would_refuse_gives_400_and_asks_nothing(spikes, monkeypatch, series):
    from core.api import store

    class Odd(store.FixtureStore):
        def item_series(self, item_id, market, days):
            return [{"item_id": item_id, "market": market, "series": series, "day": "2026-09-28", "value": 4}]

    monkeypatch.setattr(store, "get_store", Odd)
    r = spike(spikes.client, series=series)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert forwarded_asks(spikes) == []


def test_spike_needs_the_passcode_and_a_json_object(spikes):
    assert spikes.client.post("/api/spikes", json={"item_id": HERITAGE}).status_code == 401
    r = spikes.client.post("/api/spikes", json=[1], headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_spike_uses_the_ask_limits(spikes, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    assert spike(spikes.client).status_code == 202
    r = spike(spikes.client)
    assert r.status_code == 429 and r.json()["error"] == "daily_question_limit"
    r = spikes.client.post("/api/ask", json={"question": "What is behind #fixture?"}, headers=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "daily_question_limit"
    assert len(forwarded_asks(spikes)) == 1


def test_spike_counts_toward_the_ask_rate_limit(spikes):
    for _ in range(30):
        assert spike(spikes.client).status_code == 202
    r = spike(spikes.client)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert len(forwarded_asks(spikes)) == 30


def test_a_spike_the_agent_refuses_gives_the_question_back(spikes, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    agent = FastAPI()

    @agent.post("/api/ask")
    async def busy():
        return JSONResponse({"error": "rate_limited", "message": "8 asks are already running"}, status_code=429)

    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=agent), raising=False)
    r = spike(spikes.client)
    assert r.status_code == 429 and r.json()["message"] == "8 asks are already running"
    assert auth.daily_questions.counts[next(iter(auth.daily_questions.counts))][1] == 0


def test_spike_through_the_real_agent_returns_its_202(real, monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    agent_app = importlib.import_module("core.api.agent_app")
    r = spike(real.client, series="feed_tiktok")
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) == {"ask_id", "status", "events_url", "url"} and body["status"] == "running"
    held = agent_app.ASKS[body["ask_id"]]
    assert held.request["spike"]["series"] == "feed_tiktok" and held.request["tier"] == "T1"
    assert held.request["question"] == "What made #fixture_za_heritage jump in South Africa on 2026-09-28?"
    held.finished.wait(5)


@pytest.mark.parametrize("evicted", [False, True])
def test_a_spike_ask_read_back_through_f42_api_carries_its_spike(real, monkeypatch, tmp_path, evicted):
    """The spike survives f42-agent's held record and, once evicted, the runs row read back from the store."""
    from core.api import store

    agent_app = importlib.import_module("core.api.agent_app")
    spike_body = {"item_id": HERITAGE, "market": "ZA", "date": "2026-09-28", "series": "feed_tiktok"}
    r = real.client.post("/api/ask", json={"question": "What made #fixture_za_heritage jump?", "market": "ZA",
                                           "tier": "T1", "spike": spike_body}, headers=GOOD)
    assert r.status_code == 202, r.text
    ask_id = r.json()["ask_id"]
    agent_app.ASKS[ask_id].finished.wait(5)
    if evicted:
        (tmp_path / "runs.json").write_text(json.dumps([store._normalise(row) for row in agent_app.SINK]),
                                            encoding="utf-8")
        monkeypatch.setattr(store, "get_store", lambda: store.FixtureStore(tmp_path))
        agent_app.ASKS.clear()
    record = real.client.get(f"/api/ask/{ask_id}", headers=GOOD).json()
    assert record["ask_id"] == ask_id and record["spike"] == spike_body


def test_ids_with_a_trailing_newline_are_refused():
    from core.api import app as api_mod

    assert api_mod.ASK_ID_RE.match("d_1") is not None
    assert api_mod.ASK_ID_RE.match("d_1\n") is None


# Client skins (contract section 15.1): writes and reads forwarded to f42-agent like the schedules.

SKIN_BODY = {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA"], "terms": ["taxi fares"],
             "hashtags": ["#fixture_za_step"], "accounts": [], "watch_ids": [], "template": "weekly_report"}
SKIN_ROUTES = [
    ("GET", "/api/skins", None, "/api/skins", {}, 200),
    ("POST", "/api/skins", SKIN_BODY, "/api/skins", {}, 201),
    ("GET", "/api/skins/sk_1", None, "/api/skins/sk_1", {}, 200),
    ("PUT", "/api/skins/sk_1", SKIN_BODY, "/api/skins/sk_1", {}, 200),
    ("POST", "/api/skins/sk_1/archive", None, "/api/skins/sk_1/archive", {}, 201),
]


@pytest.mark.parametrize("method, path, body, agent_path, query, status", SKIN_ROUTES)
def test_skin_routes_forward_to_the_agent(ctx, method, path, body, agent_path, query, status):
    assert ctx.client.request(method, path, json=body).status_code == 401
    r = ctx.client.request(method, path, json=body, headers=GOOD)
    assert r.status_code == status
    assert r.json() == {"echo": [method, agent_path, query, body]}
    assert ctx.seen["calls"] == [(method, agent_path, query, body)]


@pytest.mark.parametrize("method, template", [
    ("GET", "/api/skins/{}"), ("PUT", "/api/skins/{}"), ("POST", "/api/skins/{}/archive"),
    ("GET", "/api/skins/{}/today"), ("POST", "/api/skins/{}/report"),
])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_skin_ids_are_checked_before_forwarding(ctx, method, template, bad):
    r = ctx.client.request(method, template.format(bad), json={}, headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen


def test_skin_writes_use_the_ask_rate_limit_and_are_not_live_questions(ctx, monkeypatch):
    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    writes = [r for r in SKIN_ROUTES if r[0] != "GET"]
    for n in range(30):
        method, path, body, *_ = writes[n % len(writes)]
        assert ctx.client.request(method, path, json=body, headers=GOOD).status_code < 400
    for method, path, body, *_ in writes:
        r = ctx.client.request(method, path, json=body, headers=GOOD)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert ctx.client.get("/api/skins", headers=GOOD).status_code == 200
    assert auth.daily_questions.counts == {}


SKIN = {"skin_id": "sk_0123456789ab", "skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA", "NG"],
        "terms": ["taxi fares"], "hashtags": ["#fixture_za_step", "owambe"], "accounts": [],
        "watch_ids": ["w_step"], "template": "weekly_report", "status": "active"}
SKIN_WATCHES = [
    {"watch_id": "w_step", "target": {"kind": "hashtag", "value": "#fixture_za_step"}, "market": "ZA",
     "rule": {"state_in": ["emerging"]}, "label": "Step", "status": "active"},
    {"watch_id": "w_other", "target": {"kind": "hashtag", "value": "#fixture_za_step"}, "market": "ZA",
     "rule": {"state_in": ["emerging"]}, "label": "Not this skin", "status": "active"}]


@pytest.fixture
def skin_ctx(ctx, monkeypatch):
    """An agent that holds one skin and two watches, and drafts investigations; Today reads the fixtures."""
    monkeypatch.delenv("F42_DATA", raising=False)
    agent = FastAPI()

    @agent.get("/api/skins/{skin_id}")
    async def one(skin_id: str):
        ctx.seen.setdefault("calls", []).append(("GET", f"/api/skins/{skin_id}", {}, None))
        if skin_id != SKIN["skin_id"]:
            return JSONResponse({"error": "not_found", "message": f"No skin {skin_id} is held here."},
                                status_code=404)
        return SKIN

    @agent.get("/api/watches")
    async def watches():
        ctx.seen.setdefault("calls", []).append(("GET", "/api/watches", {}, None))
        if ctx.seen.get("watches_status"):
            return JSONResponse({"error": "internal", "message": "x"}, status_code=ctx.seen["watches_status"])
        return {"watches": SKIN_WATCHES}

    @agent.post("/api/investigations")
    async def draft(request: Request):
        body = await request.json()
        ctx.seen.setdefault("calls", []).append(("POST", "/api/investigations", {}, body))
        return JSONResponse({"investigation_id": "i_0123456789ab", "status": "draft", "plan": {"x": 1}},
                            status_code=201)

    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=agent), raising=False)
    return ctx


def test_skin_today_is_today_narrowed_to_the_skin_with_its_alerts(skin_ctx, monkeypatch):
    from core.api import store

    class SkinTodayStore(store.FixtureStore):
        def briefs(self, date):
            rows = super().briefs(date)
            if date == "2026-09-30":
                za = next(row for row in rows if row["market"] == "ZA")
                qualified = next(card for card in za["payload"]["cards"]
                                 if card["title"] == "#fixture_za_step")
                outside = copy.deepcopy(qualified)
                outside.update(item_id="synthetic_out_of_skin", title="synthetic out of skin",
                               label="synthetic out of skin", hashtags=[])
                za["payload"]["cards"].append(outside)
            return rows

    monkeypatch.setattr(store, "get_store", lambda: SkinTodayStore())
    unscoped = skin_ctx.client.get("/api/today", headers=GOOD)
    assert unscoped.status_code == 200, unscoped.text
    unscoped_markets = unscoped.json()["markets"]
    unscoped_za = next(m for m in unscoped_markets if m["market"] == "ZA")
    unscoped_ng = next(m for m in unscoped_markets if m["market"] == "NG")
    assert any(c["item_id"] == "synthetic_out_of_skin"
               for c in unscoped_za["cards"] + unscoped_za["more"])
    r = skin_ctx.client.get("/api/skins/sk_0123456789ab/today", headers=GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == "2026-09-30"
    assert [m["market"] for m in body["markets"]] == ["ZA", "NG"]
    za, ng = body["markets"]
    assert [c["title"] for c in za["cards"]] == ["#fixture_za_step"]
    assert all(c["item_id"] != "synthetic_out_of_skin" for c in za["cards"] + za["more"])
    # "owambe" is a whole word; inside #fixture_ng_owambe it follows an underscore, so it does not match.
    assert ng["cards"] == []
    assert (za["skin_note"]["kept"], za["skin_note"]["left_out"]) == (2, 11)
    assert za["skin_note"]["kept"] + za["skin_note"]["left_out"] == (
        len(unscoped_za["cards"]) + len(unscoped_za["more"])
        + len(unscoped_za["held_back"]["items"]) + len(unscoped_za["dropped"]["items"]))
    taxi_fares = next(item for item in za["held_back"]["items"]
                      if item["item_id"] == "6170b93ed5a14be710ef21f037c67673a5c178dd666cef5a3c8b2db5c2774bb4")
    assert (taxi_fares["reason"], taxi_fares["reason_text"]) == (None, "The local why-now was not checked")
    assert [evidence["id"] for evidence in taxi_fares["evidence"]] == ["x_za_003", "rd_za_004", "fb_za_005"]
    assert {evidence["url"] for evidence in taxi_fares["evidence"]} == {
        "https://example.invalid/x/@fixture_za_3/x_za_003",
        "https://example.invalid/reddit/@fixture_za_4/rd_za_004",
        "https://example.invalid/facebook/@fixture_za_5/fb_za_005",
    }
    assert (ng["skin_note"]["kept"], ng["skin_note"]["left_out"]) == (0, 8)
    assert ng["held_back"]["items"] == []
    assert ng["skin_note"]["kept"] + ng["skin_note"]["left_out"] == (
        len(unscoped_ng["cards"]) + len(unscoped_ng["more"])
        + len(unscoped_ng["held_back"]["items"]) + len(unscoped_ng["dropped"]["items"]))
    assert body["skin"]["left_out_markets"] == ["KE"]
    assert [a["watch_id"] for a in body["alerts"]["alerts"]] == ["w_step"]
    assert body["alerts"]["alerts"][0]["fired_because"].startswith("Entered")
    assert skin_ctx.client.get("/api/skins/sk_0123456789ab/today", params={"date": "2026-09-29"},
                               headers=GOOD).json()["date"] == "2026-09-29"


def test_skin_today_errors(skin_ctx):
    assert skin_ctx.client.get("/api/skins/sk_0123456789ab/today").status_code == 401
    r = skin_ctx.client.get("/api/skins/sk_000000000000/today", headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = skin_ctx.client.get("/api/skins/sk_0123456789ab/today", params={"date": "30-09-2026"}, headers=GOOD)
    assert r.status_code == 400
    r = skin_ctx.client.get("/api/skins/sk_0123456789ab/today", params={"date": "2026-01-01"}, headers=GOOD)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    skin_ctx.seen["watches_status"] = 500
    r = skin_ctx.client.get("/api/skins/sk_0123456789ab/today", headers=GOOD)
    assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"


def test_skin_report_drafts_an_investigation_from_the_skin_and_returns_its_201(skin_ctx, monkeypatch):
    from core.api import skins

    monkeypatch.setenv("ASK_PER_IP_DAILY", "1")
    r = skin_ctx.client.post("/api/skins/sk_0123456789ab/report", headers=GOOD)
    assert r.status_code == 201
    assert r.json() == {"investigation_id": "i_0123456789ab", "status": "draft", "plan": {"x": 1}}
    drafts = [c for c in skin_ctx.seen["calls"] if c[1] == "/api/investigations"]
    assert drafts == [("POST", "/api/investigations", {}, skins.report_plan(SKIN))]
    assert not any(c[1].endswith("/start") for c in skin_ctx.seen["calls"])
    assert all(used == 0 for _, used in auth.daily_questions.counts.values())
    r = skin_ctx.client.post("/api/skins/sk_000000000000/report", headers=GOOD)
    assert r.status_code == 404
    assert len([c for c in skin_ctx.seen["calls"] if c[1] == "/api/investigations"]) == 1


def test_skin_report_uses_the_ask_rate_limit(skin_ctx):
    for _ in range(30):
        assert skin_ctx.client.post("/api/skins/sk_0123456789ab/report", headers=GOOD).status_code == 201
    r = skin_ctx.client.post("/api/skins/sk_0123456789ab/report", headers=GOOD)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


# Ask skills (contract section 15.2).


@pytest.mark.parametrize("skill", ["brand-implication", "creator-read", "context-pack"])
def test_ask_passes_a_known_skill_through_unchanged(ctx, skill):
    body = {"question": "What is behind #fixture?", "market": "ZA", "skill": skill}
    r = ctx.client.post("/api/ask", json=body, headers=GOOD)
    assert r.status_code == 202
    assert ctx.seen["calls"] == [("POST", "/api/ask", {}, body)]


@pytest.mark.parametrize("skill", ["genai-visibility", "", "BRAND-IMPLICATION", 5, ["context-pack"]])
def test_ask_refuses_an_unknown_skill_before_forwarding(ctx, skill):
    r = ctx.client.post("/api/ask", json={"question": "What is behind #fixture?", "skill": skill}, headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "calls" not in ctx.seen
    assert all(used == 0 for _, used in auth.daily_questions.counts.values())


# The whole skin report path through f42-api to the real f42-agent: the dossier names no sub-tier author.


def test_skin_report_through_the_real_agent_names_no_sub_tier_author(real, monkeypatch, tmp_path):
    from core.api import skins
    from core.api.tests.test_skins import ACCOUNTS

    monkeypatch.setitem(__import__("sys").modules, "core.collect.gdelt",
                        types.SimpleNamespace(blocked=lambda text: False))
    path = tmp_path / "skin_accounts.yaml"
    path.write_text(json.dumps(ACCOUNTS), encoding="utf-8")
    monkeypatch.setattr(skins, "ACCOUNTS_FILE", path)
    agent_app = importlib.import_module("core.api.agent_app")
    monkeypatch.setattr(agent_app, "SKINS", [])
    client = real.client
    r = client.post("/api/skins", json={**SKIN_BODY, "accounts": [{"platform": "x", "handle": "@BrandSA"}]},
                    headers=GOOD)
    assert r.status_code == 201, r.text
    skin_id = r.json()["skin_id"]
    assert client.get(f"/api/skins/{skin_id}/today", headers=GOOD).status_code == 200
    r = client.post(f"/api/skins/{skin_id}/report", headers=GOOD)
    assert r.status_code == 201, r.text
    inv_id = r.json()["investigation_id"]
    assert r.json()["status"] == "draft" and r.json()["plan"]["skin_id"] == skin_id
    assert client.post(f"/api/investigations/{inv_id}/start", headers=GOOD).status_code == 202
    stream = client.get(f"/api/investigations/{inv_id}/events", headers=GOOD)
    assert "event: done" in stream.text
    r = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}}, headers=GOOD)
    assert r.status_code == 201, r.text
    # The fixture answer's authors are micro and nano tier, and their post URLs carry their handles.
    for leak in ("fixture_za_1", "fixture_za_2"):
        assert leak not in r.text
    version = freeze_and_export(client, r.json())
    page = client.get(f"/api/dossiers/{r.json()['dossier_id']}/versions/{version}/export",
                      params={"format": "html"}, headers=GOOD).text
    for leak in ("fixture_za_1", "fixture_za_2"):
        assert leak not in page


# A skin run read through f42-api (contract 15.1): the Ask read, its export and both event streams are masked,
# whether f42-agent still holds the run or f42-api reads it back from the runs store.

def skin_run_leaks(text):
    from core.api.tests.test_skins import leaks
    return leaks(text)


@pytest.fixture
def skin_run(real, monkeypatch, tmp_path):
    """A finished skin report run on the real agent, streaming the skin fixture's post and claim."""
    from core.api import skins
    from core.api.tests.test_agent_app import skin_answer, skin_run_ask
    from core.api.tests.test_skins import ACCOUNTS

    monkeypatch.setitem(__import__("sys").modules, "core.collect.gdelt",
                        types.SimpleNamespace(blocked=lambda text: False))
    path = tmp_path / "skin_accounts.yaml"
    path.write_text(json.dumps(ACCOUNTS), encoding="utf-8")
    monkeypatch.setattr(skins, "ACCOUNTS_FILE", path)
    agent_app = importlib.import_module("core.api.agent_app")
    from core.api import investigations
    monkeypatch.setattr(agent_app, "SKINS", [])
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (investigations.fixture_planner, investigations.fixture_estimate,
                                 skin_run_ask(skin_answer())))
    client = real.client
    skin = client.post("/api/skins", json={**SKIN_BODY, "accounts": [{"platform": "x", "handle": "@BrandSA"}]},
                       headers=GOOD).json()
    inv_id = client.post(f"/api/skins/{skin['skin_id']}/report", headers=GOOD).json()["investigation_id"]
    ask_id = client.post(f"/api/investigations/{inv_id}/start", headers=GOOD).json()["ask_id"]
    assert "event: done" in client.get(f"/api/investigations/{inv_id}/events", headers=GOOD).text
    return types.SimpleNamespace(client=client, agent_app=agent_app, inv_id=inv_id, ask_id=ask_id)


def evict(run, monkeypatch):
    """Drop the run from f42-agent's memory; f42-api then reads it from the runs store."""
    from core.api import store

    record = next(json.loads(row["record"]) for row in run.agent_app.SINK
                  if row["stage"] == "ask" and json.loads(row["record"])["ask_id"] == run.ask_id)
    assert "pal_of_micro" in json.dumps(record)  # the stored record is verbatim

    class Stored(store.FixtureStore):
        def ask_record(self, ask_id):
            return copy.deepcopy(record) if ask_id == run.ask_id else None

    monkeypatch.setattr(store, "get_store", Stored)
    run.agent_app.ASKS.clear()


@pytest.mark.parametrize("evicted", [False, True])
def test_skin_run_ask_read_is_masked_through_f42_api(skin_run, monkeypatch, evicted):
    if evicted:
        evict(skin_run, monkeypatch)
    r = skin_run.client.get(f"/api/ask/{skin_run.ask_id}", headers=GOOD)
    assert r.status_code == 200 and r.json()["ask_id"] == skin_run.ask_id
    assert skin_run_leaks(r.text) == [] and "@BrandSA" in r.text


@pytest.mark.parametrize("evicted", [False, True])
def test_skin_run_export_is_masked_through_f42_api(skin_run, monkeypatch, evicted):
    if evicted:
        evict(skin_run, monkeypatch)
    r = skin_run.client.get(f"/api/ask/{skin_run.ask_id}/export", params={"format": "html"}, headers=GOOD)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert skin_run_leaks(r.text) == [] and "@BrandSA" in r.text


@pytest.mark.parametrize("evicted", [False, True])
def test_skin_run_event_streams_are_masked_through_f42_api(skin_run, monkeypatch, evicted):
    if evicted:
        evict(skin_run, monkeypatch)
    for path in (f"/api/ask/{skin_run.ask_id}/events", f"/api/investigations/{skin_run.inv_id}/events"):
        r = skin_run.client.get(path, headers=GOOD)
        assert r.status_code == 200 and "event: done" in r.text, path
        assert "event: step" in r.text, path
        assert skin_run_leaks(r.text) == [], path


def test_skin_run_investigation_read_and_list_are_masked_through_f42_api(skin_run):
    for path in (f"/api/investigations/{skin_run.inv_id}", "/api/investigations"):
        r = skin_run.client.get(path, headers=GOOD)
        assert r.status_code == 200 and skin_run_leaks(r.text) == [], path
