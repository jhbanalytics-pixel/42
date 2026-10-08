"""f42-api: the app, the passcode gate, Today and trends, and the Ask proxy.

Contract: core/api/contract.md sections 1 to 6. Today and trends are read
through core.api.today over core.api.store; every /api/ask call is forwarded
to f42-agent, over HTTP with an ID token when AGENT_URL is set, else in process.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import date as Date
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles

from core.api import auth
from core.api.auth import ApiError

logger = logging.getLogger("f42.api")

ROOT_DIR = Path(__file__).resolve().parents[2]
MARKETS = ("ZA", "NG", "KE")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")
# wait=true asks and SSE streams run up to the 3,600 second Cloud Run limit.
ASK_TIMEOUT = httpx.Timeout(3600.0)
HEALTH_TIMEOUT = httpx.Timeout(5.0)
# The open health route is reachable without a passcode, so its BigQuery-backed checks run at most once a window per
# instance however often it is asked (F2a); the time, version, cap and auth parts are read fresh each time.
HEALTH_CACHE_SECONDS = 30
_health_clock = time.monotonic
_health_cache = {"at": None, "checks": None}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
ERROR_CODES = {400: "bad_request", 401: "unauthorized", 404: "not_found", 429: "rate_limited"}
PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>42</title>"
    "<p>42 is running. The app build is not on this server yet.</p>"
)


def _web_dist() -> Path:
    return ROOT_DIR / os.environ.get("WEB_DIST", "app/web/dist")


app = FastAPI(title="f42-api", docs_url=None, redoc_url=None, openapi_url=None)
# Radar was 2.1 MB and Discover 308 KB uncompressed (F2b). Event streams are never compressed by this middleware.
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


# Errors: {"error": code, "message": plain words} everywhere (contract section 1).


def _error(status: int, code: str, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return _error(exc.status, exc.error, exc.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    code = ERROR_CODES.get(exc.status_code)
    if code is None:
        code = "internal" if exc.status_code >= 500 else "bad_request"
    return _error(exc.status_code, code, str(exc.detail), getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    parts = [
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', 'invalid')}"
        for e in exc.errors()
    ]
    return _error(400, "bad_request", "; ".join(parts) or "Bad request.")


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception):
    logger.exception("unhandled error on %s", request.url.path)
    return _error(500, "internal", "Something went wrong on the server.")


# Agent transport.


def _id_token(audience: str) -> str:
    import google.auth.transport.requests
    import google.oauth2.id_token

    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )


# AGENT_AUDIENCE: https, a lower-case host, no path, query or trailing slash.
_AUDIENCE = re.compile(r"^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_TAG_SEPARATOR = "---"


def _agent_audience(url: str) -> str:
    """The audience an ID token is minted for. It is always the canonical agent URL, never a tag URL: requests go
    to AGENT_URL, which may be a revision tag URL, and AGENT_AUDIENCE names the service itself. An unset audience
    stands only for a canonical URL (a rollback or an ordinary deploy). A tag URL with no usable audience is
    refused rather than minted for, so a tag host is never an audience. Nothing here reads the network."""
    audience = os.environ.get("AGENT_AUDIENCE", "").strip()
    host = url.removeprefix("https://")
    tagged = _TAG_SEPARATOR in host.split(".", 1)[0]
    if not audience:
        if tagged:
            raise ValueError("agent_audience_missing")
        return url
    if not _AUDIENCE.match(audience):
        raise ValueError("agent_audience_malformed")
    audience_host = audience.removeprefix("https://")
    if not tagged:
        if audience != url:
            raise ValueError("agent_audience_differs")
        return audience
    label, _, rest = host.partition(_TAG_SEPARATOR)
    if not label or "/" in host or rest != audience_host:
        raise ValueError("agent_tag_host_not_tied_to_audience")
    return audience


async def _agent_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    url = os.environ.get("AGENT_URL", "").strip().rstrip("/")
    try:
        if url.startswith("http://"):
            # Local dev: a separate agent process over plain HTTP, so SSE streams.
            # Cloud Run URLs are always https and always get an ID token.
            return httpx.AsyncClient(base_url=url, timeout=timeout)
        if url:
            try:
                audience = _agent_audience(url)
            except ValueError as exc:
                logger.warning("agent client refused: %s", exc)
                raise ApiError(502, "agent_unavailable", "The Ask service cannot be reached.") from exc
            token = await run_in_threadpool(_id_token, audience)
            return httpx.AsyncClient(
                base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
            )
        from core.api import agent_app

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_app.app),
            base_url="http://f42-agent",
            timeout=timeout,
        )
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("agent client not available: %s", type(exc).__name__)
        raise ApiError(502, "agent_unavailable", "The Ask service cannot be reached.") from exc


async def _forward(method: str, path: str, json_body=None, params=None) -> httpx.Response:
    async with await _agent_client(ASK_TIMEOUT) as client:
        try:
            return await client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            logger.warning("agent %s %s failed: %s", method, path, type(exc).__name__)
            raise ApiError(502, "agent_unavailable", "The Ask service cannot be reached.") from exc


def _passthrough(resp: httpx.Response) -> Response:
    disposition = resp.headers.get("content-disposition")
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={"Content-Disposition": disposition} if disposition else None,
    )


# Open routes.


@app.get("/api/health")
async def api_health() -> dict:
    auth_state = auth.health_status()
    # The checks run together (they used to run one after another); each answers as before.
    now = _health_clock()
    cached = _health_cache["checks"]
    if cached is None or _health_cache["at"] is None or not 0 <= now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
        cached = await asyncio.gather(
            run_in_threadpool(_bigquery_check), _agent_check(), run_in_threadpool(_today_check))
        _health_cache["at"], _health_cache["checks"] = now, cached
    bigquery, agent_health, today = cached
    agent, t2_ready = agent_health
    checks = {
        "auth": auth_state["check"],
        "bigquery": bigquery,
        "agent": agent,
        "today": today,
    }
    health_time = datetime.now(auth.SAST).replace(microsecond=0)
    from core.config import caps as caps_config

    shared_cap = caps_config.model_daily_usd(now=health_time)
    override = os.environ.get("MODEL_DAILY_USD")
    model_daily_cap_usd = min(shared_cap, float(override)) if override else shared_cap
    return {
        "ok": (
            checks["auth"] == "ok"
            and checks["bigquery"] == "ok"
            and checks["agent"] == "ok"
        ),
        "service": "f42-api",
        "version": os.environ.get("F42_VERSION", "dev"),
        "time": health_time.isoformat(timespec="seconds"),
        "model_daily_cap_usd": model_daily_cap_usd,
        "auth_mode": auth_state["auth_mode"],
        "passcode": auth_state["passcode"],
        "t2_ready": checks["agent"] == "ok" and t2_ready,
        "checks": checks,
    }


def _reset_health_cache() -> None:
    _health_cache["at"] = _health_cache["checks"] = None


def _bigquery_check() -> str:
    try:
        from core.api import store

        result = store.get_store().health()
    except Exception as exc:
        logger.warning("health: store check failed: %s", type(exc).__name__)
        return f"error: {type(exc).__name__}"
    return "ok" if result == "ok" else str(result)


def _today_check() -> str:
    try:
        from core.api import store, today

        return today.today_status(store.get_store(), auth.sast_date())
    except Exception as exc:
        logger.warning("health: today status failed: %s", type(exc).__name__)
        return "data_issue"


async def _agent_check() -> tuple[str, bool]:
    """Agent status and T2 readiness from one fresh response; readiness requires an explicit true."""
    try:
        async with await _agent_client(HEALTH_TIMEOUT) as client:
            resp = await client.get("/health")
    except (ApiError, httpx.HTTPError):
        return "unreachable", False
    if resp.status_code != 200:
        return f"status {resp.status_code}", False
    try:
        ready = resp.json().get("t2_ready") is True
    except (ValueError, AttributeError):
        ready = False
    return "ok", ready


class AuthVerifyRequest(BaseModel):
    passcode: str = ""


@app.post("/api/auth/verify")
def api_auth_verify(body: AuthVerifyRequest, request: Request) -> dict:
    """Check the configured sign-in boundary before the app opens."""
    mode = auth.configured_auth_mode()
    if mode == "iap_readonly":
        auth.verify_iap_assertion(request)
        return {"ok": True}
    if mode != "passcode":
        raise ApiError(
            503, "gate_not_configured", auth.GATE_NOT_CONFIGURED_MESSAGE
        )
    # Only failures count, so a shared office IP signing in often is never locked out.
    auth.auth_limiter.check(request, count=False)
    if not auth.passcode_matches((body.passcode or "").strip()):
        auth.auth_limiter.check(request)
        raise ApiError(401, "unauthorized", "Missing or wrong passcode.")
    return {"ok": True}


# Gated routes.

gated = APIRouter(dependencies=[Depends(auth.require_passcode)])


def _check_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if DATE_RE.match(value):
            Date.fromisoformat(value)
            return value
    except ValueError:
        pass
    raise ApiError(400, "bad_request", "date must be YYYY-MM-DD.")


def _check_market(value: str | None) -> str:
    market = (value or "").strip().upper()
    if market not in MARKETS:
        raise ApiError(400, "bad_request", "market must be ZA, NG or KE.")
    return market


def _check_ask_id(ask_id: str) -> None:
    if not ASK_ID_RE.match(ask_id):
        raise ApiError(400, "bad_request", "That is not an Ask id.")


@gated.get("/api/today")
def api_today(date: str | None = None) -> dict:
    from core.api import fast, store, today

    day = _check_date(date)
    try:
        return today.build_today(fast.prefetch(store.get_store(), fast.today_plan, day), date=day)
    except today.NotReady as exc:
        raise ApiError(
            409, "not_ready", f"Today is not published yet for {day or 'the latest date'}."
        ) from exc


@gated.get("/api/trends")
def api_trends(market: str | None = None, date: str | None = None) -> dict:
    from core.api import store, today

    mkt, day = _check_market(market), _check_date(date)
    try:
        return today.build_trends(store.get_store(), mkt, date=day)
    except today.NotReady as exc:
        raise ApiError(
            409, "not_ready", f"Today is not published yet for {day or 'the latest date'}."
        ) from exc
    except today.NotFound as exc:
        raise ApiError(404, "not_found", f"There is no {mkt} brief for {day or 'the latest date'}.") from exc


@gated.get("/api/trends/{item_id}")
def api_trend(item_id: str, market: str | None = None, date: str | None = None) -> dict:
    from core.api import store, today

    mkt, day = _check_market(market), _check_date(date)
    try:
        return today.build_trend(store.get_store(), item_id, mkt, date=day)
    except today.NotReady as exc:
        raise ApiError(
            409, "not_ready", f"Today is not published yet for {day or 'the latest date'}."
        ) from exc
    except today.NotFound as exc:
        raise ApiError(404, "not_found", "That trend is not in this brief.") from exc


def _check_item_id(item_id: str) -> None:
    if not ASK_ID_RE.match(item_id):
        raise ApiError(400, "bad_request", "That is not an item id.")


def _stage2(build):
    """Run a Stage 2 read and map its exceptions to the contract's errors (section 10)."""
    from core.api import discover

    try:
        return build()
    except discover.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except discover.NotReady as exc:
        raise ApiError(409, "not_ready", "No detection run has finished yet.") from exc
    except discover.NotFound as exc:
        raise ApiError(404, "not_found", "That item is not in the latest detection run.") from exc


@gated.get("/api/discover")
def api_discover(
    market: str | None = None,
    kind: str | None = None,
    state: str | None = None,
    platform: str | None = None,
    sort: str = "order",
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    from core.api import discover, fast, store

    mkt = (market or "").strip()
    mkt = "all" if mkt.lower() == "all" else mkt.upper()
    return _stage2(lambda: discover.build_discover(
        fast.prefetch(store.get_store(), fast.discover_plan, mkt), mkt, kind=kind or None, state=state or None,
        platform=platform or None,
        sort=sort, limit=limit, cursor=cursor or None))


@gated.get("/api/discover/radar")
def api_radar(market: str | None = None, kind: str | None = None) -> dict:
    from core.api import discover, fast, store

    mkt = (market or "").strip()
    mkt = "all" if mkt.lower() == "all" else mkt.upper()
    return _stage2(lambda: discover.build_radar(fast.prefetch(store.get_store(), fast.radar_plan, mkt), mkt,
                                                kind=kind or None))


@gated.get("/api/topics/{item_id}")
def api_topic(item_id: str, market: str | None = None) -> dict:
    from core.api import discover, fast, store

    _check_item_id(item_id)
    mkt = _check_market(market)
    return _stage2(lambda: discover.build_topic(fast.prefetch(store.get_store(), fast.topic_plan, item_id, mkt),
                                                item_id, mkt))


@gated.get("/api/compare")
def api_compare(
    mode: str | None = None,
    items: str | None = None,
    market: str | None = None,
    markets: str | None = None,
    platforms: str | None = None,
    days: int = 28,
) -> dict:
    from core.api import compare, fast, store

    try:
        held = fast.prefetch(store.get_store(), fast.compare_plan, mode, items, market, markets, platforms, days)
        return compare.build_compare(held, mode, items=items, market=market, markets=markets,
                                     platforms=platforms, days=days)
    except compare.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except compare.NotReady as exc:
        raise ApiError(409, "not_ready", f"Compare is not ready yet: {exc}.") from exc


@gated.get("/api/seed-path")
def api_seed_path(keyword: str | None = None, market: str | None = None) -> dict:
    from core.api import fast, seedpath, store

    try:
        return seedpath.build_seed_path(fast.prefetch(store.get_store(), fast.seed_path_plan, keyword, market),
                                        keyword, market)
    except seedpath.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc


@gated.get("/api/seeds")
def api_seeds(market: str | None = None) -> dict:
    from core.api import seeds, store

    try:
        return seeds.build_seeds(store.get_store(), market)
    except seeds.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc


def _v4(build):
    """Run a creators, communities or history read (contract section 12), its words carried into the error."""
    from core.api import discover, people

    try:
        return build()
    except discover.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except discover.NotReady as exc:
        raise ApiError(409, "not_ready", f"Not ready yet: {exc}.") from exc
    except discover.NotFound as exc:
        raise ApiError(404, "not_found", str(exc)) from exc
    except people.PeopleUnavailable as exc:
        raise ApiError(503, "people_unavailable", "People view unavailable.") from exc


def _optional_market(value: str | None) -> str | None:
    return _check_market(value) if (value or "").strip() else None


@gated.get("/api/creators/{creator_id}")
def api_creator(creator_id: str, market: str | None = None) -> dict:
    from core.api import people, store

    if not ASK_ID_RE.match(creator_id):
        raise ApiError(400, "bad_request", "That is not a creator id.")
    mkt = _check_market(market)
    return _v4(lambda: people.build_creator(store.get_store(), creator_id, mkt))


@gated.get("/api/communities")
def api_communities(market: str | None = None) -> dict:
    from core.api import fast, people, store

    mkt = _check_market(market)
    return _v4(lambda: people.build_communities(fast.prefetch(store.get_store(), fast.communities_plan, mkt), mkt))


@gated.get("/api/communities/{community_id}")
def api_community(community_id: str, market: str | None = None) -> dict:
    from core.api import fast, people, store

    if not ASK_ID_RE.match(community_id):
        raise ApiError(400, "bad_request", "That is not a community id.")
    mkt = _optional_market(market)
    # The page builds its market's communities as the list does, so it reads through the list's plan; with no
    # market it tries each one, sharing the cached pipeline reads between them.
    return _v4(lambda: people.build_community(
        fast.prefetch(store.get_store(), fast.communities_plan, mkt) if mkt else fast.reads(store.get_store()),
        community_id, mkt))


# Hiding a person (contract section 16): f42-web cannot write, so a hide goes to f42-agent like a watch. The list
# is read here as f42-web and fails closed like every people route. There is no lift route.


@gated.post("/api/suppressions")
async def api_suppression_create(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/suppressions", json_body=await _body(request)))


@gated.get("/api/suppressions")
def api_suppressions() -> dict:
    from core.api import store

    try:
        rows = store.get_store().suppressions()
    except Exception as exc:
        logger.warning("the suppression list could not be read (%s)", type(exc).__name__)
        rows = None
    if rows is None:
        raise ApiError(503, "people_unavailable", "People view unavailable.")
    return {"suppressions": rows}


@gated.get("/api/history/items/{item_id}")
def api_history_item(item_id: str, market: str | None = None) -> dict:
    from core.api import fast, history, store

    _check_item_id(item_id)
    mkt = _check_market(market)
    return _v4(lambda: history.build_history_item(
        fast.prefetch(store.get_store(), fast.history_item_plan, item_id, mkt), item_id, mkt))


@gated.get("/api/history/search")
def api_history_search(q: str = "", market: str | None = None) -> dict:
    from core.api import fast, history, store

    mkt = _optional_market(market)
    return _v4(lambda: history.build_history_search(
        fast.prefetch(store.get_store(), fast.history_search_plan, q, mkt), q, mkt))


@gated.get("/api/history/asks")
def api_history_asks(limit: int = 20, before: str | None = None, market: str | None = None) -> dict:
    from core.api import history, store

    mkt = _optional_market(market)
    return _v4(lambda: history.build_history_asks(store.get_store(), limit=limit, before=before or None, market=mkt))


@gated.get("/api/history/briefs")
def api_history_briefs(limit: int = 30, before: str | None = None, market: str | None = None) -> dict:
    from core.api import history, store

    mkt = _optional_market(market)
    return _v4(lambda: history.build_history_briefs(store.get_store(), limit=limit, before=before or None,
                                                    market=mkt))


@gated.get("/api/history/findings")
def api_history_findings(item_id: str | None = None, status: str | None = None,
                         market: str | None = None) -> dict:
    from core.api import history, store

    if item_id:
        _check_item_id(item_id)
    mkt = _optional_market(market)
    return _v4(lambda: history.build_history_findings(store.get_store(), item_id=item_id or None,
                                                      status=status or None, market=mkt))


@gated.get("/api/coverage")
def api_coverage(date: str | None = None) -> dict:
    from core.api import coverage, store

    day = _check_date(date) or auth.sast_date()
    return coverage.build_coverage(store.get_store(), day)


@gated.get("/api/fieldwork")
def api_fieldwork(date: str | None = None) -> dict:
    from core.api import fieldwork, store

    return fieldwork.build_fieldwork(store.get_store(), _check_date(date))


@gated.get("/api/lexicon")
def api_lexicon(market: str | None = None) -> dict:
    """Page port, 3 October 2026: the words and hashtags 42 records in one market (contract.md section 20)."""
    from core.api import lexicon, store

    try:
        return lexicon.build_lexicon(store.get_store(), market)
    except lexicon.BadRequest as exc:
        raise ApiError(400, "bad_request", str(exc)) from exc
    except lexicon.NotReady as exc:
        raise ApiError(409, "not_ready", "The lexicon fills after 42's first daily count of posts has finished.") from exc


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ApiError(400, "bad_request", "The body must be JSON.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "The body must be a JSON object.")
    return body


# Watches, feedback and alerts (contract section 10.5 and 10.6). f42-web cannot write, so writes go to
# f42-agent; they share the ask rate limit but never count as live questions.


def _check_watch_id(watch_id: str) -> None:
    if not ASK_ID_RE.match(watch_id):
        raise ApiError(400, "bad_request", "That is not a watch id.")


@gated.get("/api/watches")
async def api_watches() -> Response:
    return _passthrough(await _forward("GET", "/api/watches"))


@gated.post("/api/watches")
async def api_watch_create(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/watches", json_body=await _body(request)))


@gated.post("/api/watches/{watch_id}/pause")
async def api_watch_pause(watch_id: str, request: Request) -> Response:
    _check_watch_id(watch_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/watches/{watch_id}/pause"))


@gated.post("/api/watches/{watch_id}/resume")
async def api_watch_resume(watch_id: str, request: Request) -> Response:
    _check_watch_id(watch_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/watches/{watch_id}/resume"))


def _check_schedule_id(schedule_id: str) -> None:
    if not ASK_ID_RE.match(schedule_id):
        raise ApiError(400, "bad_request", "That is not a schedule id.")


@gated.get("/api/schedules")
async def api_schedules() -> Response:
    return _passthrough(await _forward("GET", "/api/schedules"))


@gated.post("/api/schedules")
async def api_schedule_create(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/schedules", json_body=await _body(request)))


@gated.post("/api/schedules/{schedule_id}/pause")
async def api_schedule_pause(schedule_id: str, request: Request) -> Response:
    _check_schedule_id(schedule_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/schedules/{schedule_id}/pause"))


@gated.post("/api/schedules/{schedule_id}/resume")
async def api_schedule_resume(schedule_id: str, request: Request) -> Response:
    _check_schedule_id(schedule_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/schedules/{schedule_id}/resume"))


# Client skins (contract section 15.1): writes and reads forwarded to f42-agent like the schedules. A skin's Today
# and its weekly report are built here from the skin the agent holds.


def _check_skin_id(skin_id: str) -> None:
    if not ASK_ID_RE.match(skin_id):
        raise ApiError(400, "bad_request", "That is not a skin id.")


@gated.get("/api/skins")
async def api_skins() -> Response:
    return _passthrough(await _forward("GET", "/api/skins"))


@gated.post("/api/skins")
async def api_skin_create(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/skins", json_body=await _body(request)))


@gated.get("/api/skins/{skin_id}")
async def api_skin(skin_id: str) -> Response:
    _check_skin_id(skin_id)
    return _passthrough(await _forward("GET", f"/api/skins/{skin_id}"))


@gated.put("/api/skins/{skin_id}")
async def api_skin_edit(skin_id: str, request: Request) -> Response:
    _check_skin_id(skin_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("PUT", f"/api/skins/{skin_id}", json_body=await _body(request)))


@gated.post("/api/skins/{skin_id}/archive")
async def api_skin_archive(skin_id: str, request: Request) -> Response:
    _check_skin_id(skin_id)
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/skins/{skin_id}/archive"))


async def _skin(skin_id: str) -> dict:
    resp = await _forward("GET", f"/api/skins/{skin_id}")
    if resp.status_code == 404:
        raise ApiError(404, "not_found", "No skin with that id.")
    if resp.status_code != 200:
        logger.warning("agent skin read gave status %s", resp.status_code)
        raise ApiError(502, "agent_unavailable", "The skin cannot be read right now.")
    return resp.json()


@gated.get("/api/skins/{skin_id}/today")
async def api_skin_today(skin_id: str, date: str | None = None) -> dict:
    from core.api import discover, skins, store, today

    _check_skin_id(skin_id)
    day = _check_date(date)
    skin = await _skin(skin_id)
    watches = []
    if skin.get("watch_ids"):
        resp = await _forward("GET", "/api/watches")
        if resp.status_code != 200:
            logger.warning("agent watch list gave status %s", resp.status_code)
            raise ApiError(502, "agent_unavailable", "The watch list cannot be read right now.")
        watches = [w for w in resp.json().get("watches") or [] if w.get("watch_id") in skin["watch_ids"]]

    def build():
        held = store.get_store()
        try:
            resp = today.build_today(held, date=day)
        except today.NotReady as exc:
            raise ApiError(
                409, "not_ready", f"Today is not published yet for {day or 'the latest date'}."
            ) from exc
        alerts = {"alerts": [], "waiting": []}
        if watches:
            try:
                alerts = discover.build_alerts(held, watches, date=day)
            except discover.NotReady:
                alerts = {"alerts": [], "waiting": [], "note": "No detection run has finished yet."}
        return skins.narrow_today(resp, skin, alerts)

    return await run_in_threadpool(build)


@gated.post("/api/skins/{skin_id}/report")
async def api_skin_report(skin_id: str, request: Request) -> Response:
    """The skin's weekly report as an investigation draft (contract 15.1 and 13.1). It is never started here:
    Start stays the reader's own red action, with its estimate and both budgets."""
    from core.api import skins

    _check_skin_id(skin_id)
    auth.ask_limiter.check(request)
    skin = await _skin(skin_id)
    return _passthrough(await _forward("POST", "/api/investigations", json_body=skins.report_plan(skin)))


@gated.post("/api/feedback")
async def api_feedback(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/feedback", json_body=await _body(request)))


@gated.get("/api/alerts")
async def api_alerts(date: str | None = None) -> dict:
    from core.api import discover, fast, store

    day = _check_date(date)
    # The run's reads start while the watch list comes from f42-agent; the page is built once both are in.
    held = asyncio.ensure_future(run_in_threadpool(lambda: fast.prefetch(store.get_store(), fast.alerts_plan, day)))
    held.add_done_callback(lambda f: f.cancelled() or f.exception())  # an unused failure is not logged as lost
    try:
        resp = await _forward("GET", "/api/watches")
    except BaseException:
        held.cancel()
        raise
    if resp.status_code != 200:
        held.cancel()
        logger.warning("agent watch list gave status %s", resp.status_code)
        raise ApiError(502, "agent_unavailable", "The watch list cannot be read right now.")
    watches = resp.json().get("watches") or []
    started = await held
    return await run_in_threadpool(lambda: _stage2(lambda: discover.build_alerts(started, watches, date=day)))


@gated.post("/api/ask")
async def api_ask(request: Request) -> Response:
    from core.api.skins import SKILLS

    auth.ask_limiter.check(request)
    body = await _body(request)
    # An Ask skill (contract section 15.2) is passed through unchanged; any other value is refused here.
    if body.get("skill") is not None and body["skill"] not in SKILLS:
        raise ApiError(400, "bad_request", "skill must be one of " + ", ".join(SKILLS) + ", or left out.")
    return await _ask(request, body)


async def _ask(request: Request, body: dict) -> Response:
    """Forward an ask; a live one holds one of the IP's daily questions unless the agent refuses it."""
    # Replay asks spend no live credits, so only live asks count toward the day.
    live = body.get("mode") != "replay"
    ip = auth.client_ip(request)
    if live:
        auth.daily_questions.reserve(ip)
    try:
        resp = await _forward("POST", "/api/ask", json_body=body)
    except ApiError:
        if live:
            auth.daily_questions.release(ip)
        raise
    if live and resp.status_code >= 400:
        auth.daily_questions.release(ip)
    return _passthrough(resp)


def _spike_facts(item_id: str, market: str, day: str, series: str | None) -> tuple[str, bool]:
    """(the label a topic page shows, whether the item's series has a value that day) from the store."""
    from core.api import discover, store

    held = store.get_store()
    label = item_id
    run = held.latest_detect_run()
    if run is not None:
        cards, _ = discover._cards(held, run, market)
        label = next((card["title"] for row, card, _ in cards if row["item_id"] == item_id), None) or item_id
    rows = held.item_series(item_id, market, discover.SERIES_DAYS) or []
    measured = any(str(r.get("day") or r.get("date")) == day and r.get("value") is not None
                   and series in (None, r.get("series")) for r in rows)
    return label, measured


@gated.post("/api/spikes")
async def api_spike(request: Request) -> Response:
    from core.api.spike import SERIES_RE
    from core.api.today import LABELS

    auth.ask_limiter.check(request)
    body = await _body(request)
    item_id, series = body.get("item_id"), body.get("series")
    if not isinstance(item_id, str) or not ASK_ID_RE.match(item_id):
        raise ApiError(400, "bad_request", "That is not an item id.")
    if not isinstance(body.get("market"), str):
        raise ApiError(400, "bad_request", "market must be ZA, NG or KE.")
    market = _check_market(body["market"])
    if not isinstance(body.get("date"), str):
        raise ApiError(400, "bad_request", "date must be YYYY-MM-DD.")
    day = _check_date(body["date"])
    if not (series is None or (isinstance(series, str) and SERIES_RE.fullmatch(series))):
        raise ApiError(400, "bad_request", "series must be 1 to 80 letters, digits or _, or left out.")
    label, measured = await run_in_threadpool(_spike_facts, item_id, market, day, series)
    if not measured:
        raise ApiError(400, "bad_request", "No measurement that day")
    ask = {"question": f"What made {label} jump in {LABELS[market]} on {day}?", "market": market, "tier": "T1",
           "from_card": {"item_id": item_id, "market": market, "date": day},
           "spike": {"item_id": item_id, "market": market, "date": day, "series": series}}
    return await _ask(request, ask)


async def _stored_record(ask_id: str) -> dict | None:
    """A finished Ask record from runs, as a reader sees it: a skin run's record is masked (contract 15.1) the
    way f42-agent masks the records it still holds."""
    from core.api import skins, store

    record = await run_in_threadpool(lambda: store.get_store().ask_record(ask_id))
    if not record or not record.get("skin_id"):
        return record
    skin_key = None
    if isinstance(record["skin_id"], str) and ASK_ID_RE.match(record["skin_id"]):
        resp = await _forward("GET", f"/api/skins/{record['skin_id']}")
        skin_key = resp.json().get("skin_key") if resp.status_code == 200 else None
    evidence = (record.get("answer") or {}).get("evidence") or []
    people = await run_in_threadpool(skins.people_for, skin_key, evidence, store.get_store)
    return skins.mask_people(record, people["approved"], people["allowed"])


@gated.get("/api/ask/{ask_id}")
async def api_ask_read(ask_id: str) -> Response:
    _check_ask_id(ask_id)
    resp = await _forward("GET", f"/api/ask/{ask_id}")
    if resp.status_code != 404:
        return _passthrough(resp)
    record = await _stored_record(ask_id)
    if record is None:
        raise ApiError(404, "not_found", "No Ask with that id.")
    return JSONResponse(jsonable_encoder(record))


def _sse(seq: int, event: str, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _replay_stored_events(ask_id: str, last_event_id: str | None) -> Response:
    """A finished Ask the agent no longer holds: its stored steps, then done."""
    record = await _stored_record(ask_id)
    if record is None:
        raise ApiError(404, "not_found", "No Ask with that id.")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    steps = record.get("steps") or []
    done_seq = max((s.get("seq", 0) for s in steps), default=0) + 1
    parts = [_sse(s["seq"], "step", s) for s in steps if s.get("seq", 0) > after]
    done = {"seq": done_seq, "status": record.get("status"), "url": f"/api/ask/{ask_id}"}
    parts.append(_sse(done_seq, "done", done))
    return Response("".join(parts), media_type="text/event-stream", headers=SSE_HEADERS)


async def _relay_events(path: str, request: Request, missing=None) -> Response:
    """Relay an agent event stream as it arrives; a 404 goes to `missing(last_event_id)` when given."""
    headers = {"Accept": "text/event-stream"}
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    client = await _agent_client(ASK_TIMEOUT)
    try:
        req = client.build_request("GET", path, headers=headers)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("agent events %s failed: %s", path, type(exc).__name__)
        raise ApiError(502, "agent_unavailable", "The Ask service cannot be reached.") from exc
    if resp.status_code != 200:
        await resp.aread()
        await resp.aclose()
        await client.aclose()
        if resp.status_code == 404 and missing is not None:
            return await missing(last_event_id)
        return _passthrough(resp)

    async def relay():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(relay(), media_type="text/event-stream", headers=SSE_HEADERS)


@gated.get("/api/ask/{ask_id}/events")
async def api_ask_events(ask_id: str, request: Request) -> Response:
    _check_ask_id(ask_id)
    return await _relay_events(f"/api/ask/{ask_id}/events", request,
                               lambda last_event_id: _replay_stored_events(ask_id, last_event_id))


@gated.post("/api/ask/{ask_id}/stop")
async def api_ask_stop(ask_id: str) -> Response:
    _check_ask_id(ask_id)
    return _passthrough(await _forward("POST", f"/api/ask/{ask_id}/stop"))


@gated.get("/api/ask/{ask_id}/export")
async def api_ask_export(ask_id: str, format: str = "html") -> Response:
    from core.api import export

    _check_ask_id(ask_id)
    if format != "html":
        raise ApiError(400, "bad_request", "Only format=html is available until Stage 3.")
    resp = await _forward("GET", f"/api/ask/{ask_id}")
    if resp.status_code == 200:
        record = resp.json()
    elif resp.status_code == 404:
        record = await _stored_record(ask_id)
        if record is None:
            raise ApiError(404, "not_found", "No Ask with that id.")
    else:
        return _passthrough(resp)
    try:
        page = export.render_answer_html(record)
    except export.ExportRefused as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    filename = export.export_filename(ask_id)
    return HTMLResponse(page, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# Dossiers and investigations (contract section 13): every route forwards to f42-agent, which owns the
# claims, the budgets and the tables. Writes share the ask rate limit; only an investigation start counts
# as a live question, because it is the one that spends SocialCrawl credits.


def _check_id(value: str, what: str) -> None:
    if not ASK_ID_RE.match(value):
        raise ApiError(400, "bad_request", f"That is not {what} id.")


def _known(**params) -> dict:
    return {key: value for key, value in params.items() if value is not None}


@gated.post("/api/dossiers")
async def api_dossier_create(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/dossiers", json_body=await _body(request)))


@gated.post("/api/findings")
async def api_findings_save(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/findings", json_body=await _body(request)))


@gated.get("/api/dossiers")
async def api_dossiers(limit: str | None = None, before: str | None = None) -> Response:
    return _passthrough(await _forward("GET", "/api/dossiers", params=_known(limit=limit, before=before)))


@gated.get("/api/dossiers/{dossier_id}")
async def api_dossier(dossier_id: str) -> Response:
    _check_id(dossier_id, "a dossier")
    return _passthrough(await _forward("GET", f"/api/dossiers/{dossier_id}"))


@gated.put("/api/dossiers/{dossier_id}")
async def api_dossier_edit(dossier_id: str, request: Request) -> Response:
    _check_id(dossier_id, "a dossier")
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("PUT", f"/api/dossiers/{dossier_id}", json_body=await _body(request)))


@gated.post("/api/dossiers/{dossier_id}/ticks")
async def api_dossier_tick(dossier_id: str, request: Request) -> Response:
    _check_id(dossier_id, "a dossier")
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/dossiers/{dossier_id}/ticks", json_body=await _body(request)))


@gated.post("/api/dossiers/{dossier_id}/freeze")
async def api_dossier_freeze(dossier_id: str, request: Request) -> Response:
    _check_id(dossier_id, "a dossier")
    auth.ask_limiter.check(request)
    body = await _body(request) if (await request.body()).strip() else None
    return _passthrough(await _forward("POST", f"/api/dossiers/{dossier_id}/freeze", json_body=body))


@gated.get("/api/dossiers/{dossier_id}/versions/{version}")
async def api_dossier_version(dossier_id: str, version: int) -> Response:
    _check_id(dossier_id, "a dossier")
    return _passthrough(await _forward("GET", f"/api/dossiers/{dossier_id}/versions/{version}"))


@gated.get("/api/dossiers/{dossier_id}/versions/{version}/export")
async def api_dossier_export(dossier_id: str, version: int, format: str = "html") -> Response:
    _check_id(dossier_id, "a dossier")
    return _passthrough(await _forward("GET", f"/api/dossiers/{dossier_id}/versions/{version}/export",
                                       params={"format": format}))


@gated.post("/api/investigations")
async def api_investigation_draft(request: Request) -> Response:
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", "/api/investigations", json_body=await _body(request)))


@gated.get("/api/investigations")
async def api_investigations(status: str | None = None) -> Response:
    return _passthrough(await _forward("GET", "/api/investigations", params=_known(status=status)))


@gated.get("/api/investigations/{investigation_id}")
async def api_investigation(investigation_id: str) -> Response:
    _check_id(investigation_id, "an investigation")
    return _passthrough(await _forward("GET", f"/api/investigations/{investigation_id}"))


@gated.put("/api/investigations/{investigation_id}/plan")
async def api_investigation_plan(investigation_id: str, request: Request) -> Response:
    _check_id(investigation_id, "an investigation")
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("PUT", f"/api/investigations/{investigation_id}/plan",
                                       json_body=await _body(request)))


@gated.post("/api/investigations/{investigation_id}/start")
async def api_investigation_start(investigation_id: str, request: Request) -> Response:
    _check_id(investigation_id, "an investigation")
    auth.ask_limiter.check(request)
    ip = auth.client_ip(request)
    auth.daily_questions.reserve(ip)
    try:
        resp = await _forward("POST", f"/api/investigations/{investigation_id}/start")
    except ApiError:
        auth.daily_questions.release(ip)
        raise
    if resp.status_code >= 400:
        auth.daily_questions.release(ip)
    return _passthrough(resp)


@gated.get("/api/investigations/{investigation_id}/events")
async def api_investigation_events(investigation_id: str, request: Request) -> Response:
    _check_id(investigation_id, "an investigation")
    return await _relay_events(f"/api/investigations/{investigation_id}/events", request)


@gated.post("/api/investigations/{investigation_id}/stop")
async def api_investigation_stop(investigation_id: str, request: Request) -> Response:
    _check_id(investigation_id, "an investigation")
    auth.ask_limiter.check(request)
    return _passthrough(await _forward("POST", f"/api/investigations/{investigation_id}/stop"))


app.include_router(gated)


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def api_unknown(path: str):
    raise ApiError(404, "not_found", "No such API route.")


# The built app.


class CachedStaticFiles(StaticFiles):
    """Content-hashed Vite assets: long-lived immutable cache at the edge."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Mounted only when the build exists, so the service still boots without one.
if (_web_dist() / "assets").is_dir():
    app.mount("/assets", CachedStaticFiles(directory=_web_dist() / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str) -> Response:
    """Every non-API GET: a real file at the top of dist, else index.html."""
    if full_path == "api":
        raise ApiError(404, "not_found", "No such API route.")
    dist = _web_dist()
    index = dist / "index.html"
    if not index.is_file():
        return HTMLResponse(PLACEHOLDER)
    if full_path:
        try:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
        except (OSError, ValueError):
            pass
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
