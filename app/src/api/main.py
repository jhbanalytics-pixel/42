"""FastAPI app for 42.

Routes, the passcode gate, the in-process rate limit, no-cache static serve,
and the ask pipeline orchestration. Retrieval lives in bq.py, synthesis and
the cache in synth.py, the printable one-pager in card.py.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.staticfiles import StaticFiles as _StaticFiles

from . import (
    ask_export,
    client_lenses,
    client_purpose,
    behaviours,
    bq,
    chat,
    creator,
    credit_budget,
    deployment_contract,
    desk,
    dossier_approval,
    dossier_artifacts,
    dossier_resolver,
    dossier_review_auth,
    dossier_review_store,
    dossier_store,
    fieldwork,
    intelligence_dossier,
    investigation_index,
    investigation_store,
    investigation_scopes,
    investigations,
    narrative_report,
    operator_timeline,
    pdf_exporter,
    historical_workspace,
    lexicon,
    persona_registry,
    research,
    research_role_registry,
    seed_discover,
    seeds,
    source_lab,
    synth,
    topic,
    workspace_reads,
    workspace_scope,
)
from . import card as card_render
from . import general_question_routes, general_question_startup
from .question_worker_process import configure_worker_diagnostic_logging

logger = logging.getLogger("listening_post.api")

ROOT_DIR = Path(__file__).resolve().parents[2]
# The compiled Vite build is the production UI; the single-file legacy page is
# the fallback so the service still serves if a deploy ships without dist.
WEB_DIST = ROOT_DIR / "web" / "dist"
WEB_DIST_INDEX = WEB_DIST / "index.html"
WEB_INDEX = ROOT_DIR / "web" / "index.html"

VALID_MARKETS = ("za", "ng", "ke", "all")
SINGLE_MARKETS = ("za", "ng", "ke")

RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW_SECONDS = 10.0
MAX_CHAT_HISTORY_TURNS = 20
MAX_CHAT_HISTORY_CHARS = 12000
MAX_REFINE_INSTRUCTION_CHARS = 500
# Archive search text becomes one BigQuery match clause per token, and a brief
# built from it also reaches the model, so its length is bounded before either.
MAX_SEARCH_QUERY_CHARS = 500
# Per-client hit log: one deque per client IP, pruned as windows expire so a
# single noisy client cannot starve everyone else. The dict is read and
# mutated under a lock because two requests can hit the limiter at once.
_ask_hits: dict = {}
_ask_hits_lock = threading.Lock()

# Passcode verification is a brute-force surface, so it gets its own tighter
# sliding window, separate from the ask limiter's buckets.
AUTH_RATE_LIMIT_MAX = 10
AUTH_RATE_LIMIT_WINDOW_SECONDS = 60.0
_auth_hits: dict = {}
_auth_hits_lock = threading.Lock()

# Today and rails are the same for every viewer of a market until the next
# engine run, and the engine ingests once a day (00:30 UTC cron, 02:30 UTC
# fallback). A ten-minute TTL re-scanned BigQuery ~78 times a workday for data
# that had not moved, so a warm exec never found a warm board: each endpoint
# went cold every ten minutes. The TTL now tracks the daily cadence, so the
# board holds through the workday off one fill and self-heals within the window
# if a mid-day re-ingest lands. Freshness is part of the cached payload.
MARKET_CACHE_TTL_SECONDS = 10800.0
_market_cache: dict = {}
# Per-key locks so concurrent requests for the SAME cache key serialize on the
# slow loader (a multi-second BigQuery read) instead of all missing and all
# running it. The locks dict is itself guarded by a small meta-lock; a global
# lock would serialize every cache miss across the slow read regardless of key.
_market_cache_locks: dict = {}
_market_cache_locks_meta = threading.Lock()

THIN_FLOOR = 12
THIN_READ = "Only a small evidence sample was retrieved. Read these examples with that limit."
DEGRADED_READ = (
    "Trend synthesis is unavailable right now. Every number and quote on this "
    "page comes straight from the engine."
)

PLACEHOLDER_HTML = (
    "<!DOCTYPE html>"
    '<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>42</title></head>"
    '<body style="font-family: Georgia, serif; background: #f4efe4; '
    'color: #0d0d0f; margin: 0;">'
    '<main style="max-width: 640px; margin: 12vh auto 0; padding: 0 24px;">'
    '<h1 style="letter-spacing: 0.08em;">42</h1>'
    "<p>Sub-Saharan Africa cultural trends. The interface is still being "
    "assembled. The API is live; check /api/health.</p>"
    "</main></body></html>"
)

CARD_LOCKED_HTML = (
    "<!DOCTYPE html>"
    '<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>42</title></head>"
    '<body style="font-family: Georgia, serif; background: #f4efe4; '
    'color: #0d0d0f; margin: 0;">'
    '<main style="max-width: 640px; margin: 12vh auto 0; padding: 0 24px;">'
    '<h1 style="letter-spacing: 0.08em;">42'
    '<span style="color: #ec3a1e;">.</span></h1>'
    "<p>This card needs the team passcode. Open it from the 42 "
    "interface so the passcode travels with the link.</p>"
    "</main></body></html>"
)

_RUNTIME_PROFILE_AT_STARTUP = os.environ.get("DEPLOYMENT_PROFILE", "")
deployment_contract.validate_runtime_contract()

app = FastAPI(title="42", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _configure_general_question_worker():
    configure_worker_diagnostic_logging()
    general_question_startup.configure_general_question_startup(
        app, try_acquire=_try_acquire_generation_capacity,
        release=_release_generation_capacity,
    )


# The SPA and the API ship from the same Cloud Run origin, so cross-origin
# requests are not needed for normal use. Keep the allow-list tight: the service
# Cloud Run origin and the Ogilvy host only, never a wildcard, and never a
# wildcard paired with credentials. Operators can override with ALLOWED_ORIGINS
# (comma separated) without a code change.
_DEFAULT_ALLOWED_ORIGINS = (
    "https://listening-post-590353929363.us-central1.run.app",
    "https://listening-post-staging-fibxg5ynpq-uc.a.run.app",
    "https://www.ogilvy.co.za",
    "https://ogilvy.co.za",
)


def _valid_origin(o: str) -> bool:
    # Credentials are enabled, so a wildcard or a non-TLS origin is unsafe. Allow
    # only concrete https origins (or http(s) localhost for dev). Anything else,
    # including "*", is dropped rather than trusted from the env override.
    o = o.strip().rstrip("/")
    if not o or o == "*":
        return False
    if o.startswith("https://"):
        return True
    return o.startswith(("http://localhost", "http://127.0.0.1"))


_allowed_origins = [
    o.strip().rstrip("/")
    for o in os.environ.get(
        "ALLOWED_ORIGINS", ",".join(_DEFAULT_ALLOWED_ORIGINS)
    ).split(",")
    if _valid_origin(o)
]
# Never let a bad override silently open or break CORS: fall back to the safe
# default allow-list when the override yields nothing usable.
if not _allowed_origins:
    _allowed_origins = list(_DEFAULT_ALLOWED_ORIGINS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Passcode", "Content-Type"],
    allow_credentials=True,
)


# The SPA uses inline styles and one inline theme bootstrap. Google Fonts is the
# only cross-origin asset family. Everything else stays on the service origin.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    # Deny framing and MIME sniffing, enforce the shipped resource policy, and
    # keep HTTPS sticky for the Cloud Run origin.
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(
        ("/api/v2/investigations/", "/api/internal/v2/investigations/")
    ):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "X-Passcode"
    return response


class CachedStaticFiles(_StaticFiles):
    """Content-hashed Vite assets: long-lived immutable cache at the edge."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# Vite assets are content-hashed, so they are safe to serve cacheable. The
# mount is conditional so the app still boots on a checkout without a build.
if (WEB_DIST / "assets").is_dir():
    app.mount(
        "/assets", CachedStaticFiles(directory=WEB_DIST / "assets"), name="assets"
    )


def _gate_open() -> bool:
    """True when local/dev explicitly allows running without UI_PASSCODE."""
    return os.environ.get("LP_ALLOW_OPEN_GATE", "").strip().lower() == "true"


def require_passcode(request: Request) -> None:
    """Constant-time passcode check on the X-Passcode header.

    Production deploys must mount UI_PASSCODE from Secret Manager. When it is
    unset and LP_ALLOW_OPEN_GATE is not true, gated routes fail closed."""
    expected = os.environ.get("UI_PASSCODE", "")
    if not expected:
        if _gate_open():
            return
        raise HTTPException(status_code=503, detail="Passcode gate not configured")
    supplied = request.headers.get("X-Passcode", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Missing or invalid passcode")


def _card_token(query: str, market: str) -> str:
    """A per-card auth token: HMAC(UI_PASSCODE, "<normalized query>|<market>").

    Lets a printable /card link authenticate without putting the raw passcode in
    the URL. The token is scoped to one query and market, so a leaked card link
    cannot unlock another card or the /api/* gate, and the master secret never
    appears in logs or history. No expiry, so the card stays forwardable.
    """
    secret = os.environ.get("UI_PASSCODE", "")
    msg = f"{query}|{market}".encode()
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _client_ip(request: Request) -> str:
    # Cloud Run's front-end appends the real caller IP as the RIGHTMOST
    # X-Forwarded-For hop. Earlier (leftmost) hops are client-supplied and
    # spoofable, so the rate limiter must key on the rightmost entry, else a
    # caller can mint a fresh bucket per request and defeat the Vertex-cost cap.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_rate_limit(request: Request) -> None:
    now = time.monotonic()
    client_ip = _client_ip(request)
    with _ask_hits_lock:
        for ip in list(_ask_hits):
            hits = _ask_hits[ip]
            while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
                hits.popleft()
            if not hits:
                del _ask_hits[ip]
        hits = _ask_hits.setdefault(client_ip, deque())
        if len(hits) >= RATE_LIMIT_MAX:
            logger.warning("rate limit tripped by client %s", client_ip)
            raise HTTPException(
                status_code=429,
                detail="Rate limit reached: 30 requests per 10 seconds. Try again shortly.",
            )
        hits.append(now)


def _bounded_search_text(value: str, field: str) -> str:
    """Refuse search text over the limit before it reaches BigQuery or the model."""
    if len(value) > MAX_SEARCH_QUERY_CHARS:
        raise HTTPException(status_code=400, detail=f"{field} is too long")
    return value


def _trim_chat_history(history: list) -> list:
    """Cap chat history length so one send cannot blow the Vertex prompt budget."""
    if not isinstance(history, list):
        return []
    trimmed = history[-MAX_CHAT_HISTORY_TURNS:]
    out: list = []
    total = 0
    for turn in trimmed:
        if not isinstance(turn, dict):
            continue
        blob = json.dumps(turn, default=str)
        if total + len(blob) > MAX_CHAT_HISTORY_CHARS and out:
            break
        out.append(turn)
        total += len(blob)
    return out


RESEARCH_GENERATE_MAX_DEFAULT = 30
RESEARCH_BATCH_GENERATE_MAX_DEFAULT = 10
RESEARCH_REFINE_MAX = 5
RESEARCH_BEHAVIOURS_MAX = 15
RESEARCH_LIMIT_WINDOW_SECONDS = 600.0
_research_hits: dict = {}
_research_hits_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _research_generate_cap() -> int:
    return _env_int("RESEARCH_GENERATE_MAX", RESEARCH_GENERATE_MAX_DEFAULT)


def _research_batch_generate_cap() -> int:
    return _env_int("RESEARCH_BATCH_GENERATE_MAX", RESEARCH_BATCH_GENERATE_MAX_DEFAULT)


def _check_research_rate_limit(request: Request, action: str) -> None:
    now = time.monotonic()
    client_ip = _client_ip(request)
    if action == "generate":
        cap = _research_generate_cap()
    elif action == "batch_generate":
        cap = _research_batch_generate_cap()
    elif action == "behaviours":
        cap = RESEARCH_BEHAVIOURS_MAX
    else:
        cap = RESEARCH_REFINE_MAX
    bucket_key = (client_ip, action)
    with _research_hits_lock:
        hits = _research_hits.get(bucket_key)
        if hits is None:
            hits = deque()
            _research_hits[bucket_key] = hits
        while hits and now - hits[0] > RESEARCH_LIMIT_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= cap:
            raise HTTPException(
                status_code=429,
                detail=f"Research rate limit reached for {action}. Try again shortly.",
            )
        hits.append(now)


def _validate_market(market: str) -> str:
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=400, detail="market must be za, ng, ke, or all")
    return market


def build_ask_payload(query: str, market: str) -> dict:
    """Retrieve and project archive evidence without starting generation."""
    raw = bq.search_content(query, market)
    # Clean the wide retrieval pool, rank it by how fully each row matches the
    # ask (so a broadened OR retry leads with the on-point rows, not generic
    # single-token hits), then cap the evidence pool used to select quotes.
    # Retrieval aggregates remain separate from that smaller selection.
    items = bq.rank_by_query(bq.clean_items(raw["items"]), query)[:40]
    series = bq.zero_filled_series(raw["daily_counts"])

    platform_split = [{"platform": p, "n": int(n)} for p, n in raw["platform_split"]]
    market_split = [{"market": m, "n": int(n)} for m, n in raw["market_split"]]
    # Only named accounts. A list of masked "<Platform> creator" rows adds no
    # value, so anon ids are dropped rather than shown as faceless repeats.
    creators = [
        {"handle": bq.mask_handle(handle, platform), "mentions": int(n)}
        for handle, platform, n in raw["creators"]
        if bq.is_real_handle(handle, platform)
    ][:8]
    quotes = bq.build_quotes(items)

    thin = len(items) < THIN_FLOOR

    return {
        "query": query,
        "market": market,
        "total_matches": int(raw["total_matches"]),
        "thin": thin,
        "broad": bool(raw.get("broad")),
        "volume_series": series,
        "platform_split": platform_split,
        "market_split": market_split,
        "trends": [],
        "quotes": quotes,
        "slang": [],
        "creators": creators,
        "overall_read": THIN_READ if thin else "",
        "limited_voice": len(quotes) < 3,
    }


def _ask_payload_cached(query: str, market: str) -> dict:
    key = (query, market)
    cached = synth.cache_get(key)
    if cached is not None:
        return cached
    payload = build_ask_payload(query, market)
    synth.cache_set(key, payload)
    return payload


def _market_cache_ttl_seconds(kind: str) -> float:
    """Desk payloads track the engine's daily run; cache them until UTC midnight."""
    if kind == "desk":
        now = datetime.now(UTC)
        end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return max(60.0, (end - now).total_seconds())
    return MARKET_CACHE_TTL_SECONDS


def _market_cached(kind: str, market: str, loader, cacheable=None) -> dict:
    """TTL cache with per-key locking. `cacheable`, when given, decides whether
    a fresh payload may be stored: error or degraded payloads are transient
    outages, and caching one would pin the failure for the whole TTL."""
    key = (kind, market)
    ttl = _market_cache_ttl_seconds(kind)
    now = time.monotonic()
    entry = _market_cache.get(key)
    if entry is not None and now - entry[0] < ttl:
        return entry[1]
    # Fetch the per-key lock under the meta-lock so two requests for the same key
    # share one lock, then run the loader inside it. The first request loads; the
    # rest wait on this key alone and pick up its result from the double check.
    with _market_cache_locks_meta:
        lock = _market_cache_locks.setdefault(key, threading.Lock())
    with lock:
        entry = _market_cache.get(key)
        now = time.monotonic()
        if entry is not None and now - entry[0] < ttl:
            return entry[1]
        payload = loader()
        if cacheable is None or cacheable(payload):
            _market_cache[key] = (now, payload)
        return payload


def _ingest_cache_key() -> str:
    try:
        return bq.fetch_ingest_cache_version()
    except Exception:
        logger.warning("ingest cache version query failed", exc_info=True)
        return "0:0:0"


def _with_live_freshness(payload: dict) -> dict:
    """Serve a cached desk payload with its clock-derived fields recomputed.

    The payload is cached until UTC midnight because the engine writes once a
    day, which is right for the data and wrong for one field inside it.
    Freshness is a function of NOW, so the cached copy froze at build time and
    reported the same age, and the same green, all day. That is precisely the
    indicator that has to go amber when a run dies, so it is re-derived here on
    every serve, and the per-topic age chips with it.
    """
    if not isinstance(payload, dict) or "freshness" not in payload:
        return payload
    fresh = bq.refresh_freshness(payload.get("freshness"))
    out = dict(payload)
    out["freshness"] = fresh
    label = desk.age_label(fresh.get("age_hours"))
    topics = payload.get("topics")
    if isinstance(topics, list):
        out["topics"] = [
            dict(t, age=label) if isinstance(t, dict) and "age" in t else t
            for t in topics
        ]
    return out


def _desk_payload_cacheable(payload: dict) -> bool:
    """A desk payload whose pan-African surface failed with a retryable error
    is served but not stored, so a refused probe or a timed-out read is retried
    on the next request instead of being pinned until UTC midnight. Every
    other surface, and a non-retryable stories failure, is cached as before."""
    error = (payload.get("pan_african_surface") or {}).get("error") or {}
    return not error.get("retryable", False)


def _desk_cached(region: str, loader):
    payload = _market_cached(
        "desk",
        f"{region}|{_ingest_cache_key()}",
        loader,
        cacheable=_desk_payload_cacheable,
    )
    return _with_live_freshness(payload)


def _metrics_cached(loader):
    return _market_cached("metrics", f"all|{_ingest_cache_key()}", loader)


@app.get("/api/health")
def api_health() -> dict:
    # Surface data freshness so an operator can detect engine staleness
    # (and the DEGRADED_READ synthesis path) from the health check. Wrapped
    # in the same TTL cache as the data payloads so polling does not run a
    # BQ query per hit. The probe is fail-open: a BQ outage must not turn the
    # health check itself red, so a failed freshness read reports amber.
    try:
        # The BigQuery read stays cached; the age and status it implies do not,
        # so the cached stamp is re-derived against the clock on every request.
        freshness = bq.refresh_freshness(
            _market_cached("health_freshness", "all", bq._freshness)
        )
    except Exception:
        freshness = {"stamp_utc": None, "age_hours": None, "status": "amber"}
    configured = bool(os.environ.get("UI_PASSCODE"))
    allow_open = _gate_open()
    return {
        "ok": configured or allow_open,
        "dataset": os.environ.get("BQ_DATASET", "trends_v2_dev"),
        "passcode": configured,
        "freshness": freshness,
    }


class AuthVerifyRequest(BaseModel):
    passcode: str = ""


class InvestigationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str | None = None
    audience_lens_ids: tuple[str, ...] = ()
    theme_id: str | None = None
    run_id: str
    contract_version: str
    decision_question: str
    time_horizon_days: int
    brand_context: str | None = None
    known_assumptions: tuple[str, ...] = ()
    change_my_mind_if: tuple[str, ...] = ()
    research_role_id: str | None = None
    research_role_version: str | None = None
    output_mode: str = "internal_working_paper"
    # Omitted or null is general 42. A named lens is resolved under the
    # resolved scope before any storage read; it never widens the scope.
    client_lens_id: StrictStr | None = None


def _check_auth_rate_limit(request: Request) -> None:
    # Same sliding-window shape as the ask limiter, tighter cap: 10 attempts
    # per minute per client IP so the passcode cannot be brute-forced.
    now = time.monotonic()
    client_ip = _client_ip(request)
    with _auth_hits_lock:
        for ip in list(_auth_hits):
            hits = _auth_hits[ip]
            while hits and now - hits[0] > AUTH_RATE_LIMIT_WINDOW_SECONDS:
                hits.popleft()
            if not hits:
                del _auth_hits[ip]
        hits = _auth_hits.setdefault(client_ip, deque())
        if len(hits) >= AUTH_RATE_LIMIT_MAX:
            logger.warning("auth rate limit tripped by client %s", client_ip)
            raise HTTPException(
                status_code=429,
                detail="Too many passcode attempts. Wait a minute and try again.",
            )
        hits.append(now)


@app.post("/api/auth/verify")
def api_auth_verify(body: AuthVerifyRequest, request: Request) -> dict:
    """Check a passcode before the client stores it in localStorage."""
    _check_auth_rate_limit(request)
    expected = os.environ.get("UI_PASSCODE", "")
    if not expected:
        if _gate_open():
            return {"ok": True}
        raise HTTPException(status_code=503, detail="Passcode gate not configured")
    supplied = (body.passcode or "").strip()
    if not supplied:
        raise HTTPException(status_code=400, detail="passcode is required")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Missing or invalid passcode")
    return {"ok": True}


def _new_investigation_storage_client():
    from google.cloud import storage

    return storage.Client(project=os.environ.get("GCP_PROJECT") or None)


def _investigation_bucket():
    try:
        bucket = _new_investigation_storage_client().bucket(
            "listening-post-staging-cache"
        )
    except Exception as error:
        raise deployment_contract.DeploymentContractError(
            "investigation storage client is unavailable"
        ) from error
    if getattr(bucket, "name", None) != "listening-post-staging-cache":
        raise deployment_contract.DeploymentContractError(
            "investigation bucket is invalid"
        )
    return bucket


def _investigation_now() -> datetime:
    return datetime.now(UTC)


def _active_research_role_versions() -> dict[str, str]:
    return {
        role.role_id: role.role_version
        for role in research_role_registry.list_available_roles(
            approved_role_digests=research_role_registry.APPROVED_ROLE_DIGESTS
        )
    }


def _validate_investigation_runtime() -> None:
    if _RUNTIME_PROFILE_AT_STARTUP != deployment_contract.STAGING_PROFILE:
        raise deployment_contract.DeploymentContractError(
            "investigation endpoint requires the staging startup profile"
        )
    deployment_contract.validate_runtime_contract(
        metadata_email_getter=lambda: deployment_contract.REQUIRED_SERVICE_ACCOUNT
    )


def _investigation_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _source_lab_error(
    status_code: int, code: str, guard: str, **values: object
) -> HTTPException:
    """The refusal the caller sees, and the line that says why it refused.

    The public detail stays the four approved codes and their non-revealing
    messages. The reason goes to the log instead, where the guard that refused
    is named. Nothing here carries a credential: the passcode is consumed by
    the gate dependency and never reaches this handler.
    """
    messages = {
        "contract_version_unsupported": "Source Lab contract version is unsupported.",
        "scope_invalid": "Source Lab is unavailable for this scope.",
        "catalog_snapshot_invalid": "Source Lab is unavailable for this scope.",
        "source_lab_unavailable": "Source Lab is unavailable for this scope.",
    }
    detail = " ".join(f"{key}={value!r}" for key, value in values.items())
    logger.error(
        "source lab refused the request: code=%s status=%s guard=%s %s",
        code,
        status_code,
        guard,
        detail,
    )
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": messages[code]}
    )


def _source_lab_current_date():
    return datetime.now(UTC).date()


def _validate_source_lab_runtime() -> None:
    if _RUNTIME_PROFILE_AT_STARTUP != deployment_contract.STAGING_PROFILE:
        raise deployment_contract.DeploymentContractError(
            "Source Lab requires the open intelligence staging startup profile"
        )
    if os.environ.get("BQ_DATASET") != "trends_v2_staging":
        raise deployment_contract.DeploymentContractError(
            "Source Lab requires the staging BigQuery dataset"
        )
    deployment_contract.validate_runtime_contract(
        metadata_email_getter=lambda: deployment_contract.REQUIRED_SERVICE_ACCOUNT
    )


@app.get("/api/v2/source-lab")
def api_source_lab(
    contract_version: str = Query(...),
    _gate: None = Depends(require_passcode),
) -> Response:
    if contract_version not in source_lab.SUPPORTED_CONTRACT_VERSIONS:
        raise _source_lab_error(
            400,
            "contract_version_unsupported",
            "handler.contract_version",
            requested=contract_version,
            supported=sorted(source_lab.SUPPORTED_CONTRACT_VERSIONS),
        )
    try:
        _validate_source_lab_runtime()
    except deployment_contract.DeploymentContractError as error:
        raise _source_lab_error(
            503,
            "source_lab_unavailable",
            "handler.runtime_contract",
            reason=str(error),
        ) from error
    scope = source_lab.fixed_scope()
    try:
        current_metric_date = _source_lab_current_date()
        rows = bq.fetch_source_lab_rows(
            scope.client_scope_id,
            current_metric_date=current_metric_date,
            dataset=os.environ["BQ_DATASET"],
        )
        payload = source_lab.build_response(
            scope=scope,
            rows=rows,
            current_metric_date=current_metric_date,
        )
        if contract_version == source_lab.CONTRACT_VERSION_V2:
            payload = source_lab.build_versioned_response(
                payload,
                contract_version=contract_version,
                credit_budget_reading=credit_budget.read_credit_budget(),
            )
        return JSONResponse(content=source_lab.json_ready(payload))
    except source_lab.SourceLabUnavailable as error:
        raise _source_lab_error(
            503,
            "source_lab_unavailable",
            "handler.source_lab_unavailable",
            contract_version=contract_version,
        ) from error
    except source_lab.CatalogSnapshotInvalid as error:
        raise _source_lab_error(
            503,
            "catalog_snapshot_invalid",
            "handler.catalog_snapshot_invalid",
            contract_version=contract_version,
        ) from error
    except Exception as error:
        logger.error("source lab read failed", exc_info=True)
        raise _source_lab_error(
            503,
            "source_lab_unavailable",
            "handler.unexpected_error",
            error_type=type(error).__name__,
        ) from error


@app.post("/api/v2/investigations")
def api_create_investigation(
    body: InvestigationCreateRequest,
    _gate: None = Depends(require_passcode),
) -> dict:
    try:
        _validate_investigation_runtime()
    except deployment_contract.DeploymentContractError as error:
        raise _investigation_error(
            503,
            "investigation_runtime_invalid",
            "Investigation runtime is unavailable.",
        ) from error
    try:
        resolved_scope = investigation_scopes.resolve_investigation_scope(
            client_scope_id=body.client_scope_id,
            market_scope=body.market_scope,
            brand_config_id=body.brand_config_id,
            audience_lens_ids=body.audience_lens_ids,
            theme_id=body.theme_id,
        )
    except investigation_scopes.InvestigationScopeInvalid as error:
        raise _investigation_error(
            404, "scope_invalid", "Investigation scope is invalid."
        ) from error
    # The lens is resolved the way /api/chat/send resolves it. A lens of
    # another client scope, an unknown lens and a disabled lens are refused as
    # the scope is, and nothing is created in their place.
    try:
        client_lens = client_lenses.resolve_client_lens(
            client_scope_id=resolved_scope.client_scope_id,
            brand_config_id=resolved_scope.brand_config_id,
            client_lens_id=body.client_lens_id,
        )
    except client_lenses.ClientLensUnavailable as error:
        raise _investigation_error(
            404, "scope_invalid", "Investigation scope is invalid."
        ) from error
    try:
        frame = investigations.InvestigationFrame(
            client_scope_id=resolved_scope.client_scope_id,
            market_scope=resolved_scope.market_scope,
            brand_config_id=resolved_scope.brand_config_id,
            audience_lens_ids=resolved_scope.audience_lens_ids,
            theme_id=resolved_scope.theme_id,
            run_id=body.run_id,
            contract_version=body.contract_version,
            decision_question=body.decision_question,
            time_horizon_days=body.time_horizon_days,
            brand_context=body.brand_context,
            known_assumptions=body.known_assumptions,
            change_my_mind_if=body.change_my_mind_if,
            research_role_id=body.research_role_id,
            research_role_version=body.research_role_version,
            output_mode=body.output_mode,
            client_lens=None if client_lens is None else client_lens.envelope,
        )
    except ValueError as error:
        if str(error) == "contract_version_unsupported":
            raise _investigation_error(
                400,
                "contract_version_unsupported",
                "Investigation contract version is unsupported.",
            ) from error
        if "research role" in str(error):
            raise _investigation_error(
                404, "scope_invalid", "Investigation scope is invalid."
            )
        raise _investigation_error(
            400, "investigation_invalid", "Investigation is invalid."
        )

    try:
        active_role_versions = _active_research_role_versions()
    except research_role_registry.ResearchRoleInvalidError as error:
        raise _investigation_error(
            404, "scope_invalid", "Investigation scope is invalid."
        ) from error

    try:
        bucket = _investigation_bucket()
    except deployment_contract.DeploymentContractError as error:
        raise _investigation_error(
            503,
            "investigation_runtime_invalid",
            "Investigation runtime is unavailable.",
        ) from error
    if bucket is None:
        raise _investigation_error(
            503,
            "investigation_storage_unavailable",
            "Investigation storage is unavailable.",
        )
    prefix = os.environ.get("CACHE_PREFIX", "")
    investigation_id = investigations.investigation_id_for_frame(frame)
    try:
        existing = investigation_store.read_investigation(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
        )
    except (ValueError, investigation_store.InvestigationStoreError) as error:
        raise _investigation_error(
            503,
            "investigation_storage_unavailable",
            "Investigation storage is unavailable.",
        ) from error
    if existing is not None:
        return existing["response"]

    try:
        record = investigations.build_plan_ready(
            frame=frame,
            plan=investigations.default_plan_for_frame(frame),
            investigation_id=investigation_id,
            created_at=_investigation_now(),
            active_role_versions=active_role_versions,
        )
    except ValueError as error:
        if "research role version" in str(error):
            raise _investigation_error(
                404, "scope_invalid", "Investigation scope is invalid."
            )
        raise _investigation_error(
            400, "investigation_invalid", "Investigation is invalid."
        )
    try:
        storage_record = investigations.build_investigation_storage_record(
            frame, record
        )
        investigation_store.create_investigation(
            bucket=bucket,
            prefix=prefix,
            record=storage_record,
        )
    except investigation_store.InvestigationConflict:
        try:
            existing = investigation_store.read_investigation(
                bucket=bucket,
                prefix=prefix,
                investigation_id=investigation_id,
            )
        except (ValueError, investigation_store.InvestigationStoreError) as error:
            raise _investigation_error(
                503,
                "investigation_storage_unavailable",
                "Investigation storage is unavailable.",
            ) from error
        if existing is not None:
            return existing["response"]
        raise _investigation_error(
            409,
            "investigation_conflict",
            "Investigation creation conflicted with an existing record.",
        )
    except (ValueError, investigation_store.InvestigationStoreError) as error:
        raise _investigation_error(
            503,
            "investigation_storage_unavailable",
            "Investigation storage is unavailable.",
        ) from error
    return record


_WORKSPACE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "X-Passcode",
}


def _workspace_http_error(error: workspace_scope.WorkspaceScopeError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


async def _resolved_workspace_request(
    request: Request, investigation_id: str
) -> workspace_scope.ResolvedWorkspaceScope:
    if await request.body():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "workspace_request_invalid",
                "message": "Workspace request is invalid.",
            },
        )
    try:
        return workspace_scope.resolve_workspace_scope(investigation_id)
    except workspace_scope.WorkspaceScopeError as error:
        raise _workspace_http_error(error) from error


def _workspace_response(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(content=payload, headers=_WORKSPACE_HEADERS)


def _workspace_error_response(
    status_code: int, detail: dict[str, object]
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
        headers=_WORKSPACE_HEADERS,
    )


# Dossier review: login binding, session, same origin and CSRF
#
# The principal a decision records is resolved here from the server session and
# never from JSON. A login starts with a nonce and a CSRF token the server
# issues; the credential the identity provider returns must carry that nonce,
# the token must come back both in the body and in the cookie the login set,
# and the pair binds once into a session held in a Secure, HttpOnly,
# SameSite=Strict cookie. Every review command then needs the same origin, that
# session, and the session's own CSRF token in a header a cross-origin page
# cannot send. Refusals name the check, never the value that failed it, and
# every refusal in this layer happens before any storage is read.

REVIEW_LOGIN_MAX_AGE_SECONDS = _env_int("REVIEW_LOGIN_MAX_AGE_SECONDS", 300)
REVIEW_SESSION_MAX_AGE_SECONDS = _env_int("REVIEW_SESSION_MAX_AGE_SECONDS", 3600)
REVIEW_CSRF_COOKIE = "g_csrf_token"
REVIEW_SESSION_COOKIE = "review_session"
REVIEW_CSRF_HEADER = "X-Review-CSRF"
REVIEW_COOKIE_PATH = "/api/internal/v2"
_RESEARCH_ARTIFACT_ID = re.compile(r"ra_[0-9a-f]{16}\Z")
_review_logins: dict[str, dict] = {}
_review_sessions: dict[str, dict] = {}
_review_bound_nonces: set[str] = set()
_review_lock = threading.Lock()


class _ReviewRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def _review_refusal(refused: "_ReviewRefused") -> JSONResponse:
    return _workspace_error_response(
        refused.status, {"code": refused.code, "message": refused.message}
    )


def _review_now() -> float:
    return time.time()


def _review_recorded_at() -> datetime:
    return datetime.now(UTC)


def _review_configuration() -> dict | None:
    """The deployment's review expectations, or None when any of them is missing."""
    audience = os.environ.get("REVIEW_OAUTH_AUDIENCE", "").strip()
    issuer = os.environ.get(
        "REVIEW_OAUTH_ISSUER", "https://accounts.google.com"
    ).strip()
    hosted_domain = os.environ.get("REVIEW_HOSTED_DOMAIN", "").strip()
    raw = os.environ.get("REVIEW_SUBJECT_ALLOWLIST", "").strip()
    try:
        allowlist = json.loads(raw) if raw else None
    except ValueError:
        allowlist = None
    # An empty allowlist is a configuration: sign-in works and every subject
    # is refused and logged for enrolment, and no command can be authorised.
    if (
        not audience
        or not issuer
        or not hosted_domain
        or not isinstance(allowlist, dict)
        or not all(
            type(subject) is str
            and subject
            and dossier_review_auth._listed_roles(roles) is not None
            for subject, roles in allowlist.items()
        )
    ):
        return None
    return {
        "audience": audience,
        "issuer": issuer,
        "hosted_domain": hosted_domain,
        "allowlist": allowlist,
    }


def _review_authority() -> dossier_approval.AuthorityState:
    """Admitted only while identity, roles and the create-only store are all configured."""
    configuration = _review_configuration()
    if configuration is None or not configuration["allowlist"]:
        return dossier_approval.AuthorityState(
            available=False,
            code=dossier_review_auth.AUTHORITY_UNAVAILABLE,
            reason="Review is not configured for this deployment.",
        )
    return dossier_approval.AuthorityState(
        available=True,
        code="review_authority_admitted",
        reason=(
            "Identity is validated from the credential flow, the role is mapped "
            "from the subject allowlist and decisions are stored create-only."
        ),
    )


def _verify_review_credential(credential: str, audience: str) -> dict:
    """The pinned verifier: signature, audience, issuer and expiry, returning the claims."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(credential, google_requests.Request(), audience)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin", "").strip().rstrip("/")
    if not _valid_origin(origin):
        return False
    host = request.headers.get("host", "").strip().lower()
    if not host or urlsplit(origin).netloc.lower() != host:
        return False
    site = request.headers.get("sec-fetch-site")
    return site is None or site.strip().lower() == "same-origin"


def _require_same_origin(request: Request) -> None:
    if not _same_origin(request):
        raise _ReviewRefused(
            400, dossier_review_auth.REQUEST_INVALID, "origin_mismatch"
        )


def _set_review_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=REVIEW_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _review_principal(request: Request) -> dict:
    """The session's principal after origin, session and CSRF; nothing here reads storage."""
    _require_same_origin(request)
    return _review_session_principal(request)


def _review_session_principal(request: Request) -> dict:
    """The session's principal after fetch site, session and CSRF, without the Origin check.

    A browser sends no Origin on a same-origin GET, so a review read cannot
    demand one. Sec-Fetch-Site, when the browser sends it, must still say
    same-origin, and the session and its CSRF header are required exactly as
    they are for a command. Nothing here reads storage.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() != "same-origin":
        raise _ReviewRefused(
            400, dossier_review_auth.REQUEST_INVALID, "origin_mismatch"
        )
    if _review_configuration() is None:
        raise _ReviewRefused(
            503, dossier_review_auth.AUTHORITY_UNAVAILABLE, "configuration_missing"
        )
    session_id = request.cookies.get(REVIEW_SESSION_COOKIE, "")
    with _review_lock:
        session = _review_sessions.get(session_id) if session_id else None
        if session is not None and not _review_now() < session["expires_at"]:
            del _review_sessions[session_id]
            session = None
    if session is None:
        raise _ReviewRefused(
            403, dossier_review_auth.ROLE_REQUIRED, "no principal holds a role"
        )
    presented = request.headers.get(REVIEW_CSRF_HEADER, "")
    if not presented:
        raise _ReviewRefused(400, dossier_review_auth.REQUEST_INVALID, "csrf_missing")
    if not hmac.compare_digest(
        presented.encode("utf-8"), session["csrf_token"].encode("utf-8")
    ):
        raise _ReviewRefused(400, dossier_review_auth.REQUEST_INVALID, "csrf_mismatch")
    return dict(session["principal"])


async def _review_json(request: Request) -> object:
    try:
        return json.loads(await request.body())
    except ValueError:
        raise _ReviewRefused(
            400, dossier_review_auth.REQUEST_INVALID, "review request is not JSON"
        ) from None


@app.post("/api/internal/v2/dossier/review/login", include_in_schema=False)
async def api_dossier_review_login(
    request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """Start one login: a nonce for the credential and a CSRF token in a cookie."""
    try:
        _require_same_origin(request)
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    if _review_configuration() is None:
        return _workspace_error_response(
            503,
            {
                "code": dossier_review_auth.AUTHORITY_UNAVAILABLE,
                "message": "configuration_missing",
            },
        )
    nonce = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = _review_now()
    with _review_lock:
        for token in [
            token
            for token, issued in _review_logins.items()
            if now > issued["issued_at"] + REVIEW_LOGIN_MAX_AGE_SECONDS
        ]:
            del _review_logins[token]
        _review_logins[csrf_token] = {"nonce": nonce, "issued_at": now}
    response = JSONResponse(
        {
            "contract_version": "dossier_review_login_v1",
            "nonce": nonce,
            "csrf_token": csrf_token,
            "expires_in": REVIEW_LOGIN_MAX_AGE_SECONDS,
        },
        headers=_WORKSPACE_HEADERS,
    )
    _set_review_cookie(
        response, REVIEW_CSRF_COOKIE, csrf_token, REVIEW_LOGIN_MAX_AGE_SECONDS
    )
    return response


def _principal_roles(principal: dict) -> list[str]:
    """Every role the principal holds; a subject listed with one role holds that one."""
    return list(principal.get("roles", [principal["role"]]))


_ENROLMENT_SUBJECT = re.compile(r"[A-Za-z0-9_-]{1,255}")
_ENROLMENT_HOSTED_DOMAIN = re.compile(r"[A-Za-z0-9.-]{1,253}")


def _refuse_for_enrolment(issued: dict, cookie_token: str, claims: dict, body: dict, now) -> None:
    """Refuse a verified subject that is not allowlisted, after the login binding held.

    The credential has passed the signature, audience, issuer, expiry and
    hosted domain checks. The nonce and CSRF token are then bound as for any
    login, which also spends the login, so only a complete sign-in reaches
    the log. The one line names the subject and the signed hosted domain so
    the subject can be enrolled; the token, email and name never appear.
    Nothing is written to storage and no session is created.
    """
    try:
        with _review_lock:
            dossier_review_auth.bind_session(
                nonce=issued["nonce"],
                csrf_token=cookie_token,
                issued_at=issued["issued_at"],
                now=now,
                max_age_seconds=REVIEW_LOGIN_MAX_AGE_SECONDS,
                seen=_review_bound_nonces,
                presented_nonce=claims.get("nonce"),
                presented_csrf_token=body["g_csrf_token"],
            )
            _review_logins.pop(cookie_token, None)
    except dossier_review_auth.ReviewAuthError as error:
        raise _ReviewRefused(error.status, error.code, error.reason) from None
    subject, hosted_domain = claims.get("sub"), claims.get("hd")
    if (
        type(subject) is str
        and _ENROLMENT_SUBJECT.fullmatch(subject) is not None
        and type(hosted_domain) is str
        and _ENROLMENT_HOSTED_DOMAIN.fullmatch(hosted_domain) is not None
    ):
        logger.warning(
            "review_enrolment_refused subject=%s hd=%s", subject, hosted_domain
        )
    else:
        # A value outside the identifier grammar could forge log fields, so
        # the line says only that an unlisted subject was refused.
        logger.warning("review_enrolment_refused subject=invalid")
    raise _ReviewRefused(
        403, dossier_review_auth.ROLE_REQUIRED, "subject_not_allowlisted"
    )


@app.post("/api/internal/v2/dossier/review/session", include_in_schema=False)
async def api_dossier_review_session(
    request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """Bind the returned credential to one session, once.

    The body carries the credential and the CSRF token the identity provider's
    flow posts back; the cookie the login set must hold the same token. The
    claims come from the pinned verifier, the role from the allowlist, and the
    nonce inside the credential must be the one this login issued.
    """
    try:
        _require_same_origin(request)
        configuration = _review_configuration()
        if configuration is None:
            raise _ReviewRefused(
                503, dossier_review_auth.AUTHORITY_UNAVAILABLE, "configuration_missing"
            )
        body = await _review_json(request)
        if (
            type(body) is not dict
            or set(body) != {"credential", "g_csrf_token"}
            or type(body["credential"]) is not str
            or not body["credential"]
            or type(body["g_csrf_token"]) is not str
        ):
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "login_malformed"
            )
        cookie_token = request.cookies.get(REVIEW_CSRF_COOKIE, "")
        if not cookie_token:
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "csrf_missing"
            )
        if not hmac.compare_digest(
            cookie_token.encode("utf-8"), body["g_csrf_token"].encode("utf-8")
        ):
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "csrf_mismatch"
            )
        now = _review_now()
        with _review_lock:
            issued = _review_logins.get(cookie_token)
        if issued is None:
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "login_unknown"
            )
        try:
            claims = _verify_review_credential(
                body["credential"], configuration["audience"]
            )
        except Exception:
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "credential_invalid"
            ) from None
        try:
            principal = dossier_review_auth.validate_review_credential(
                token_claims=claims,
                now=now,
                expected_audience=configuration["audience"],
                expected_issuer=configuration["issuer"],
                expected_hd=configuration["hosted_domain"],
                subject_allowlist=configuration["allowlist"],
                enrolment=True,
            )
        except dossier_review_auth.ReviewAuthError as error:
            if error.reason != "subject_not_allowlisted":
                raise _ReviewRefused(error.status, error.code, error.reason) from None
            _refuse_for_enrolment(issued, cookie_token, claims, body, now)
        try:
            with _review_lock:
                bound = dossier_review_auth.bind_session(
                    nonce=issued["nonce"],
                    csrf_token=cookie_token,
                    issued_at=issued["issued_at"],
                    now=now,
                    max_age_seconds=REVIEW_LOGIN_MAX_AGE_SECONDS,
                    seen=_review_bound_nonces,
                    presented_nonce=claims.get("nonce"),
                    presented_csrf_token=body["g_csrf_token"],
                )
                _review_logins.pop(cookie_token, None)
        except dossier_review_auth.ReviewAuthError as error:
            raise _ReviewRefused(error.status, error.code, error.reason) from None
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    session_csrf = secrets.token_urlsafe(32)
    expires_at = now + REVIEW_SESSION_MAX_AGE_SECONDS
    with _review_lock:
        _review_sessions[bound["session_id"]] = {
            "principal": principal,
            "csrf_token": session_csrf,
            "nonce_digest": bound["nonce_digest"],
            "bound_at": bound["bound_at"],
            "expires_at": expires_at,
        }
    response = JSONResponse(
        {
            "contract_version": "dossier_review_session_v1",
            "role": principal["role"],
            "roles": _principal_roles(principal),
            "csrf_token": session_csrf,
            "expires_at": expires_at,
        },
        headers=_WORKSPACE_HEADERS,
    )
    _set_review_cookie(
        response,
        REVIEW_SESSION_COOKIE,
        bound["session_id"],
        REVIEW_SESSION_MAX_AGE_SECONDS,
    )
    response.delete_cookie(
        REVIEW_CSRF_COOKIE,
        path=REVIEW_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


# The review command, the artifact prepare and the versioned artifact read


def _review_scope(investigation_id: str) -> workspace_scope.ResolvedWorkspaceScope:
    try:
        return workspace_scope.resolve_workspace_scope(investigation_id)
    except workspace_scope.WorkspaceScopeError as error:
        if error.status_code == 503:
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
        raise _ReviewRefused(404, "scope_invalid", "scope is unavailable") from None


def _review_bucket():
    try:
        return _investigation_bucket()
    except deployment_contract.DeploymentContractError:
        raise _ReviewRefused(
            503, "review_storage_unavailable", "review storage failed"
        ) from None


def _review_dossier(
    scope: workspace_scope.ResolvedWorkspaceScope, bucket, dossier_version: str
) -> dict:
    """The exact version named, through the resolver, or the registered refusal."""
    resolution = dossier_resolver.resolve_dossier(scope, dossier_version, bucket=bucket)
    if resolution["state"] == "ready":
        return resolution["dossier"]
    reason = resolution["reason"]
    if reason == "dossier_storage_unavailable":
        raise _ReviewRefused(503, "review_storage_unavailable", "review storage failed")
    if reason == "dossier_scope_mismatch":
        raise _ReviewRefused(404, "scope_invalid", "scope is unavailable")
    if reason == "dossier_request_invalid":
        raise _ReviewRefused(
            400, dossier_review_auth.REQUEST_INVALID, "dossier version is invalid"
        )
    raise _ReviewRefused(
        409, "review_version_conflict", "dossier version is not available"
    )


def _artifact_decision_binding(decision: dict, manifest: dict) -> dict:
    """A decision as the artifact predicate compares it.

    The decision pins the artifact version, and the artifact version is the
    digest over the scope, selection and export digests, so those three are
    read from the manifest the decision names; the predicate then compares
    investigation, dossier version and artifact version field by field.
    """
    return {
        "state": decision["state"],
        "investigation_id": decision["investigation_id"],
        "dossier_version": decision["dossier_version"],
        "artifact_version": decision["resource_version"],
        "scope_digest": manifest["scope_digest"],
        "selection_digest": manifest["selection_digest"],
        "export_digests": manifest["export_digests"],
    }


def _validated_v2_artifact_review(
    *, bucket, prefix: str, scope, artifact: dict, pointer: dict
) -> tuple[dict, bool]:
    """Replay only the pointer's committed artifact decisions, in pointer order."""
    entry = dossier_review_store.resource_pointer_state(
        pointer, resource="artifact", resource_id=artifact["artifact_id"]
    )
    if (
        entry["resource_version"] != artifact["artifact_version"]
        or not entry["applied"]
    ):
        raise dossier_store.DossierRecordInvalid("artifact_review_unreadable")
    state = dossier_approval.initial_state("artifact")
    decisions = []
    for decision_id in entry["applied"]:
        decision = dossier_review_store.read_decision(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            dossier_version=artifact["dossier_version"],
            decision_id=decision_id,
        )
        if (
            decision["investigation_id"] != scope.investigation_id
            or decision["dossier_version"] != artifact["dossier_version"]
            or decision["resource"] != "artifact"
            or decision["resource_id"] != artifact["artifact_id"]
            or decision["resource_version"] != artifact["artifact_version"]
            or decision["expected_state"] != state
        ):
            raise dossier_store.DossierRecordInvalid("artifact_review_unreadable")
        try:
            transition = dossier_approval.plan_transition(
                "artifact", state, decision["action"]
            )
        except dossier_approval.ApprovalTransitionUnknown:
            raise dossier_store.DossierRecordInvalid(
                "artifact_review_unreadable"
            ) from None
        if (
            decision["next_state"] != transition.next_state
            or decision["state"] != transition.next_state
        ):
            raise dossier_store.DossierRecordInvalid("artifact_review_unreadable")
        state = transition.next_state
        decisions.append(decision)
    if state != entry["state"]:
        raise dossier_store.DossierRecordInvalid("artifact_review_unreadable")
    approved = state == "approved" and dossier_artifacts.is_artifact_approved(
        manifest=artifact["manifest"],
        decision=_artifact_decision_binding(decisions[-1], artifact["manifest"]),
    )
    if state == "approved" and not approved:
        raise dossier_store.DossierRecordInvalid("artifact_review_unreadable")
    return entry, approved


def _check_resource_version(*, bucket, scope, dossier: dict, command: dict) -> None:
    """The command's resource_version must be the content digest the record holds.

    Claims and relationships are projected by the resolver; an artifact is the
    stored record whose id the command names, for this scope and version.
    """
    resource, resource_id = command["resource"], command["resource_id"]
    if resource == "artifact":
        artifact = dossier_artifacts.read_artifact(
            bucket=bucket,
            prefix=dossier_store.STAGING_PREFIX,
            investigation_id=scope.investigation_id,
            artifact_id=resource_id,
        )
        expected = (
            artifact["artifact_version"]
            if artifact is not None
            and artifact["dossier_version"] == command["dossier_version"]
            and artifact["manifest"]["scope_digest"] == scope.scope_digest
            else None
        )
    else:
        expected = dossier_resolver.resource_versions(dossier)[resource].get(resource_id)
    if expected != command["resource_version"]:
        raise _ReviewRefused(409, "review_version_conflict", "resource_version_mismatch")


def _scope_purpose_policy(scope: workspace_scope.ResolvedWorkspaceScope) -> dict | None:
    """The client purpose policy the scope's brand names, None for general 42."""
    try:
        return client_purpose.load_client_purpose_policy(scope.frame.brand_config_id)
    except client_purpose.ClientPurposePolicyUnavailable:
        raise _ReviewRefused(404, "scope_invalid", "scope is unavailable") from None


def _refuse_artifact_purpose_override(*, bucket, scope, principal: dict, command: dict) -> None:
    """A review on an artifact whose content serves a prohibited client purpose is refused.

    The artifact cannot have been prepared under the policy, so this guards a
    record prepared before it or elsewhere; the refusal ignores the role.
    """
    if command["resource"] != "artifact":
        return
    policy = _scope_purpose_policy(scope)
    if policy is None:
        return
    artifact = dossier_artifacts.read_artifact(
        bucket=bucket,
        prefix=dossier_store.STAGING_PREFIX,
        investigation_id=scope.investigation_id,
        artifact_id=command["resource_id"],
    )
    if artifact is None:
        return
    evaluation = client_purpose.evaluate_client_purpose(
        policy, client_purpose.client_read_texts(artifact["client_read"])
    )
    try:
        dossier_review_store.refuse_client_purpose_override(
            evaluation, role=principal.get("role"), action=command["action"]
        )
    except dossier_store.DossierRecordInvalid as refused:
        raise _ReviewRefused(403, refused.code, "client purpose is prohibited") from None


def _record_review(*, bucket, scope, principal: dict, command: dict) -> dict:
    """One decision through record_review, which stores it and commits the state pointer."""
    try:
        return dossier_approval.record_review(
            bucket=bucket,
            scope=scope,
            principal=principal,
            command=command,
            authority=_review_authority(),
            now=_review_recorded_at(),
        )
    except dossier_approval.ReviewRefused as refused:
        raise _ReviewRefused(
            refused.refusal.status, refused.refusal.code, refused.refusal.reason
        ) from None


def _timeline_body(response: Response) -> dict:
    try:
        value = json.loads(response.body)
    except (AttributeError, TypeError, ValueError):
        return {}
    return value if type(value) is dict else {}


def _emit_artifact_outcome(
    response: Response,
    *,
    stage: str,
    action: str,
    investigation_id: str | None = None,
    request_id: str | None = None,
    artifact_id: str | None = None,
    artifact_version: str | None = None,
    format: str | None = None,
) -> None:
    """One operator timeline event for an artifact route, read from its own response.

    Only identifiers, digests, the review state and the refusal code are taken;
    artifact bytes, answers and sources in the body never reach the event.
    """
    try:
        body = _timeline_body(response)
        completed = 200 <= response.status_code < 300
        detail = body.get("detail")
        operator_timeline.emit_artifact_event(
            stage=stage,
            action=action,
            event="completed" if completed else "failed",
            request_id=request_id,
            investigation_id=investigation_id,
            artifact_id=body.get("artifact_id", artifact_id) if completed else artifact_id,
            artifact_version=(
                body.get("artifact_version", artifact_version)
                if completed
                else None
            ),
            dossier_version=body.get("dossier_version") if completed else None,
            format=body.get("format", format) if completed else format,
            state=body.get("state") if completed else None,
            reason_code=None
            if completed
            else detail.get("code")
            if type(detail) is dict
            else None,
        )
    except Exception:
        logger.warning("question_artifact_event_unavailable")


@app.post(
    "/api/internal/v2/investigations/{investigation_id}/dossier/review",
    include_in_schema=False,
)
async def api_dossier_review(
    investigation_id: str, request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """One human decision on one exact resource, with an artifact decision on the timeline."""
    # Only the command the inner handler validated, after origin, session and
    # CSRF were settled, may name an event; this wrapper never reads the body.
    validated: dict = {}
    response = await _dossier_review(investigation_id, request, validated)
    command = validated.get("command")
    if (
        type(command) is dict
        and command.get("resource") == "artifact"
        and command.get("action") in ("submit", "approve", "reject")
    ):
        _emit_artifact_outcome(
            response,
            stage="artifact_review",
            action=command["action"],
            investigation_id=investigation_id,
            artifact_id=command.get("resource_id"),
            artifact_version=command.get("resource_version"),
            format="html",
        )
    return response


async def _dossier_review(
    investigation_id: str, request: Request, validated: dict
) -> Response:
    """One human decision on one exact resource of the current dossier version.

    Origin, session and CSRF are settled before the body is read, the body must
    be exactly the registered command, the scope must resolve, and the
    resource_version must be the content digest the exact version holds before
    the store records anything. The result is the registered five fields.
    """
    try:
        principal = _review_principal(request)
        body = await _review_json(request)
        try:
            command = dossier_review_store.validate_review_command(body)
        except dossier_store.DossierRecordInvalid:
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "review command is invalid"
            ) from None
        validated["command"] = command
        scope = _review_scope(investigation_id)
        bucket = _review_bucket()
        try:
            dossier = _review_dossier(scope, bucket, command["dossier_version"])
            _check_resource_version(
                bucket=bucket, scope=scope, dossier=dossier, command=command
            )
            _refuse_artifact_purpose_override(
                bucket=bucket, scope=scope, principal=principal, command=command
            )
        except (dossier_store.DossierStoreError, dossier_artifacts.ArtifactInvalid):
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
        result = _record_review(
            bucket=bucket, scope=scope, principal=principal, command=command
        )
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    return JSONResponse(result, headers=_WORKSPACE_HEADERS)


def _artifact_result(artifact: dict, state: str) -> dict:
    return {
        "contract_version": artifact["contract_version"],
        "investigation_id": artifact["investigation_id"],
        "dossier_version": artifact["dossier_version"],
        "artifact_id": artifact["artifact_id"],
        "artifact_version": artifact["artifact_version"],
        "state": state,
    }


@app.post(
    "/api/internal/v2/investigations/{investigation_id}/artifacts/prepare",
    include_in_schema=False,
)
async def api_artifact_prepare(
    investigation_id: str, request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """Prepare and submit the artifact, and record the preparation on the timeline."""
    response = await _artifact_prepare(investigation_id, request)
    _emit_artifact_outcome(
        response,
        stage="artifact_preparation",
        action="prepare",
        investigation_id=investigation_id,
        format="html",
    )
    return response


async def _artifact_prepare(investigation_id: str, request: Request) -> Response:
    """Prepare the HTML artifact of the current dossier version and submit it for review.

    The artifact is the Client Read payload projected from the version's
    committed claim and relationship decisions, the standalone HTML rendered
    from it, and the manifest with both byte digests. It is stored create-only
    under an id derived from its own content, and the editor's submit decision
    moves it to pending_review through the same record and pointer path the
    review command uses. Preparing identical content again returns the same
    artifact in whatever state its review has reached. A v1 artifact also
    carries a PDF made from those same stored HTML bytes by the reviewed
    renderer, stored beside the record before it, with its digest in the
    manifest; see _prepared_v1_artifact.
    """
    try:
        principal = _review_principal(request)
        body = await _review_json(request)
        narrative = (
            type(body) is dict
            and body.get("contract_version")
            == dossier_artifacts.ARTIFACT_CONTRACT_VERSION_V2
        )
        valid_v1 = (
            type(body) is dict
            and set(body) == {"contract_version", "dossier_version", "format"}
            and body["contract_version"] == dossier_artifacts.ARTIFACT_CONTRACT_VERSION
            and body["format"] in dossier_artifacts.FORMATS
        )
        valid_v2 = (
            narrative
            and set(body)
            == {
                "contract_version",
                "dossier_version",
                "format",
                "content_kind",
                "preset_id",
            }
            and body["format"] == "html"
            and body["content_kind"] == dossier_artifacts.NARRATIVE_CONTENT_KIND
            and body["preset_id"] == narrative_report.GENERAL_PRESET_ID
        )
        if not valid_v1 and not valid_v2:
            raise _ReviewRefused(
                400,
                "narrative_request_invalid" if narrative else dossier_review_auth.REQUEST_INVALID,
                "artifact request is invalid",
            )
        try:
            # The store's own statement of a well formed version name, before
            # any storage is read.
            dossier_store.dossier_object_name(
                dossier_store.STAGING_PREFIX, investigation_id, body["dossier_version"]
            )
        except ValueError:
            raise _ReviewRefused(
                400,
                "narrative_request_invalid" if narrative else dossier_review_auth.REQUEST_INVALID,
                "artifact request is invalid",
            ) from None
        if "dossier_editor" not in _principal_roles(principal):
            raise _ReviewRefused(
                403, dossier_review_auth.ROLE_REQUIRED, "role cannot prepare an artifact"
            )
        scope = _review_scope(investigation_id)
        if narrative and scope.frame.brand_config_id is not None:
            raise _ReviewRefused(
                503, "identity_assets_unavailable", "identity assets are unavailable"
            )
        bucket = _review_bucket()
        prefix = dossier_store.STAGING_PREFIX
        version = body["dossier_version"]
        try:
            dossier = _review_dossier(scope, bucket, version)
            current = dossier_store.read_pointer(
                bucket=bucket, prefix=prefix, investigation_id=scope.investigation_id
            )
            if current is None:
                raise _ReviewRefused(
                    409, "review_version_conflict", "no dossier version is current"
                )
            if current["pointer"]["scope_digest"] != scope.scope_digest:
                raise _ReviewRefused(404, "scope_invalid", "scope is unavailable")
            if current["pointer"]["dossier_version"] != version:
                raise _ReviewRefused(
                    409, "review_version_conflict", "dossier version is not current"
                )
            state = dossier_review_store.read_state_pointer(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                dossier_version=version,
            )
            decisions = dossier_review_store.list_decisions(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                dossier_version=version,
            )
            # The payload describes the content being approved; its own
            # approval lives in the manifest binding, not in these bytes.
            applied = [
                item
                for item in dossier_review_store.applied_decisions(decisions, state["pointer"])
                if item["resource"] != "artifact"
            ]
            client_read = intelligence_dossier.read_dossier(
                scope.investigation_id, dossier_resolver.projection_record(dossier, applied)
            )
            build_arguments = {
                "investigation_id": scope.investigation_id,
                "scope_digest": scope.scope_digest,
                "dossier_version": version,
                "selected_claim_ids": (
                    [claim["claim_id"] for claim in client_read["claims"]]
                    if narrative
                    else dossier["review_state"].get("selected_claim_ids") or []
                ),
                "client_read": client_read,
                "format": body["format"],
                "purpose_policy": _scope_purpose_policy(scope),
            }
            if narrative:
                build_arguments.update(
                    content_kind=dossier_artifacts.NARRATIVE_CONTENT_KIND,
                    preset=narrative_report.general_preset(scope.frame.client_scope_id),
                )
            else:
                # The client file names the investigation by its question, and
                # the frame's lens in the Ask export's words for this scope.
                build_arguments["title"] = scope.frame.decision_question
                build_arguments["client_lens"] = scope.frame.client_lens
                build_arguments["client_scope_id"] = scope.frame.client_scope_id
            pdf = None
            try:
                if narrative:
                    artifact = dossier_artifacts.build_artifact(**build_arguments)
                else:
                    artifact, pdf = await _prepared_v1_artifact(
                        bucket=bucket, prefix=prefix, arguments=build_arguments
                    )
            except dossier_artifacts.ArtifactInvalid:
                if narrative:
                    raise _ReviewRefused(
                        400,
                        "narrative_request_invalid",
                        "narrative request is invalid",
                    ) from None
                raise
            entry = dossier_review_store.resource_pointer_state(
                state["pointer"], resource="artifact", resource_id=artifact["artifact_id"]
            )
            if entry["state"] != dossier_approval.initial_state("artifact"):
                stored = dossier_artifacts.read_artifact(
                    bucket=bucket,
                    prefix=prefix,
                    investigation_id=scope.investigation_id,
                    artifact_id=artifact["artifact_id"],
                )
                if stored != artifact:
                    raise _ReviewRefused(
                        409, "review_version_conflict", "artifact bytes differ"
                    )
                return JSONResponse(
                    _artifact_result(artifact, entry["state"]), headers=_WORKSPACE_HEADERS
                )
            if pdf is not None:
                dossier_artifacts.create_artifact_pdf(
                    bucket=bucket, prefix=prefix, artifact=artifact, pdf=pdf
                )
            dossier_artifacts.create_artifact(bucket=bucket, prefix=prefix, artifact=artifact)
        except dossier_store.DossierConflict:
            raise _ReviewRefused(
                409, "review_version_conflict", "artifact already exists differently"
            ) from None
        except client_purpose.ClientPurposeProhibited as refused:
            raise _ReviewRefused(403, refused.code, "client purpose is prohibited") from None
        except (
            dossier_store.DossierStoreError,
            dossier_artifacts.ArtifactInvalid,
            intelligence_dossier.DossierContractError,
        ):
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
        submit = {
            "contract_version": dossier_review_store.REVIEW_CONTRACT_VERSION,
            "dossier_version": version,
            "resource": "artifact",
            "resource_id": artifact["artifact_id"],
            "resource_version": artifact["artifact_version"],
            "action": "submit",
            "expected_state": entry["state"],
            "idempotency_key": "prepare:" + artifact["artifact_id"],
            "support_review": None,
        }
        result = _record_review(
            bucket=bucket, scope=scope, principal=principal, command=submit
        )
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    return JSONResponse(_artifact_result(artifact, result["state"]), headers=_WORKSPACE_HEADERS)


# The dossier selection, the working read, the v1 artifact exports and the
# approved export reads
#
# A selection is a new immutable dossier version: the current record with the
# editor's selected claim ids, naming the current version as its predecessor,
# published through the same create-only store and generation-checked pointer
# as any version. Decisions belong to the version they were made on, so a new
# selection starts with no review and nothing approved earlier carries over.
# The earlier version and every artifact prepared from it stay stored and stay
# readable at their exact versions.
#
# An export read serves the stored bytes of one artifact at one exact version,
# for the scope the server resolved, only when the committed review of that
# version is an approval bound to exactly that manifest. Everything else about
# the artifact, absent, another scope, another version, is refused with the
# code an absent artifact gets.
#
# The working read is the internal view a reviewer acts from. It carries
# pending and rejected material, so it needs the review session and a
# contract role as every command does. It is a GET, which a browser sends
# without an Origin header, so it checks Sec-Fetch-Site, the session cookie
# and the session's CSRF header, but not Origin.

SELECTION_STATE = "draft"
_SELECTION_LIMIT = 256
_EXPORT_MEDIA_TYPES = {"html": "text/html; charset=utf-8", "pdf": "application/pdf"}


def _selection_ids(body: object) -> list[str] | None:
    """The request's selected claim ids in the order given, or None for any other grammar."""
    if (
        type(body) is not dict
        or set(body) != {"contract_version", "expected_dossier_version", "selected_claim_ids"}
        or body["contract_version"] != dossier_review_store.REVIEW_CONTRACT_VERSION
        or not dossier_store._digest(body["expected_dossier_version"])
    ):
        return None
    ids = body["selected_claim_ids"]
    if (
        type(ids) is not list
        or len(ids) > _SELECTION_LIMIT
        or not all(dossier_review_store._bounded_text(item) for item in ids)
        or len(set(ids)) != len(ids)
    ):
        return None
    return list(ids)


@app.post(
    "/api/internal/v2/investigations/{investigation_id}/dossier/selection",
    include_in_schema=False,
)
async def api_dossier_selection(
    investigation_id: str, request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """Save the editor's claim selection as a new immutable dossier version.

    The expected version must be the current one, and every id must name a
    reviewable claim of it. Saving the selection the current version already
    holds writes nothing and returns that version.
    """
    try:
        principal = _review_principal(request)
        body = await _review_json(request)
        selected = _selection_ids(body)
        if selected is None:
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "selection request is invalid"
            )
        if "dossier_editor" not in _principal_roles(principal):
            raise _ReviewRefused(
                403, dossier_review_auth.ROLE_REQUIRED, "role cannot change the selection"
            )
        scope = _review_scope(investigation_id)
        bucket = _review_bucket()
        prefix = dossier_store.STAGING_PREFIX
        expected = body["expected_dossier_version"]
        try:
            record = _review_dossier(scope, bucket, expected)
            current = dossier_store.read_pointer(
                bucket=bucket, prefix=prefix, investigation_id=scope.investigation_id
            )
            if current is None:
                raise _ReviewRefused(
                    409, "review_version_conflict", "no dossier version is current"
                )
            if current["pointer"]["scope_digest"] != scope.scope_digest:
                raise _ReviewRefused(404, "scope_invalid", "scope is unavailable")
            if current["pointer"]["dossier_version"] != expected:
                raise _ReviewRefused(
                    409, "review_version_conflict", "dossier version is not current"
                )
            known = dossier_resolver.resource_versions(record)["claim"]
            if any(claim_id not in known for claim_id in selected):
                raise _ReviewRefused(
                    400, dossier_review_auth.REQUEST_INVALID, "selection names an unknown claim"
                )
            version = expected
            # Compared in order on purpose: the selection digest an artifact
            # binds is taken over the ids in the order the record keeps them,
            # so a reordered selection is a different selection.
            if record["review_state"].get("selected_claim_ids") != selected:
                created_at = dossier_approval._recorded_at(_review_recorded_at())
                successor = dict(
                    record,
                    review_state=dict(record["review_state"], selected_claim_ids=selected),
                    predecessor_version=expected,
                    created_at=created_at,
                )
                created = dossier_store.create_dossier(
                    bucket=bucket,
                    prefix=prefix,
                    body=dossier_store._canonical(successor),
                    publication={
                        "source_binding_digest": record["bindings"]["source_binding_digest"],
                        "publisher_ref": principal["principal_ref"],
                        "publisher_build_digest": record["bindings"]["publisher_build_digest"],
                        "created_at": created_at,
                        "authority_ref": "dossier_review_v1:selection",
                    },
                    pointer_writer=lambda pointer: dossier_store.publish_pointer(
                        pointer, bucket=bucket, prefix=prefix
                    ),
                )
                version = created["publication"]["dossier_version"]
        except dossier_store.DossierPointerFailed:
            raise _ReviewRefused(
                409, "review_version_conflict", "dossier version is not current"
            ) from None
        except dossier_store.DossierConflict:
            raise _ReviewRefused(
                409, "review_version_conflict", "dossier version already exists differently"
            ) from None
        except dossier_store.DossierStoreError:
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    return JSONResponse(
        {
            "contract_version": dossier_review_store.REVIEW_CONTRACT_VERSION,
            "investigation_id": scope.investigation_id,
            "dossier_version": version,
            "decision_id": None,
            "state": SELECTION_STATE,
        },
        headers=_WORKSPACE_HEADERS,
    )


def _render_artifact_pdf(html_bytes: bytes) -> bytes:
    """The reviewed renderer, reading the stored HTML inside the approved asset root."""
    return pdf_exporter.render_stored_html_pdf(
        html_bytes, asset_root=ask_export.PDF_ASSET_ROOT
    )


async def _prepared_v1_artifact(*, bucket, prefix: str, arguments: dict) -> tuple[dict, bytes | None]:
    """The v1 artifact for this content with its PDF, or the one already prepared.

    A selected claim that cannot be cited refuses before anything is
    rendered. Content that was prepared before is returned as stored, with no
    PDF to write, so preparing again never renders again. Otherwise the PDF
    is made off the event loop from the exact HTML the record stores; a
    renderer refusal is a plain 503 and nothing has been written.
    """
    client_read = arguments["client_read"]
    if "claim_not_cited" in client_read["artifact_readiness"]["failed"]:
        raise _ReviewRefused(
            409, "artifact_citation_missing", "a selected claim has no usable citation"
        )
    content = {
        "investigation_id": arguments["investigation_id"],
        "scope_digest": arguments["scope_digest"],
        "dossier_version": arguments["dossier_version"],
        "selected_claim_ids": arguments["selected_claim_ids"],
        "client_read": client_read,
    }
    found = dossier_artifacts.find_prepared_artifact(
        bucket=bucket,
        prefix=prefix,
        html=dossier_artifacts.render_html(
            client_read,
            title=arguments["title"],
            client_lens=arguments["client_lens"],
            client_scope_id=arguments["client_scope_id"],
        ),
        **content,
    )
    if found is not None:
        return found, None
    try:
        built = await asyncio.to_thread(
            lambda: dossier_artifacts.build_artifact_exports(
                **content,
                purpose_policy=arguments["purpose_policy"],
                render_pdf=_render_artifact_pdf,
                title=arguments["title"],
                client_lens=arguments["client_lens"],
                client_scope_id=arguments["client_scope_id"],
            )
        )
    except pdf_exporter.PdfExportError as error:
        logger.warning("artifact pdf export refused reason=%s", error.reason)
        raise _ReviewRefused(
            503, "review_storage_unavailable", "export could not be created"
        ) from None
    return built["artifact"], built["pdf"]


def _approved_export_decision(*, bucket, prefix: str, artifact: dict) -> bool:
    """Whether the committed review of the artifact's own version approves exactly it."""
    pointer = dossier_review_store.read_state_pointer(
        bucket=bucket,
        prefix=prefix,
        investigation_id=artifact["investigation_id"],
        dossier_version=artifact["dossier_version"],
    )["pointer"]
    entry = dossier_review_store.resource_pointer_state(
        pointer, resource="artifact", resource_id=artifact["artifact_id"]
    )
    if (
        entry["state"] != "approved"
        or not entry["applied"]
        or entry["resource_version"] != artifact["artifact_version"]
    ):
        return False
    decision = dossier_review_store.read_decision(
        bucket=bucket,
        prefix=prefix,
        investigation_id=artifact["investigation_id"],
        dossier_version=artifact["dossier_version"],
        decision_id=entry["applied"][-1],
    )
    return (
        decision["state"] == "approved"
        and decision["resource"] == "artifact"
        and decision["resource_id"] == artifact["artifact_id"]
        and dossier_artifacts.is_artifact_approved(
            manifest=artifact["manifest"],
            decision=_artifact_decision_binding(decision, artifact["manifest"]),
        )
    )


async def _artifact_export(
    request: Request, investigation_id: str, artifact_id: str, extension: str
) -> Response:
    scope = await _resolved_workspace_request(request, investigation_id)
    version = _single_query_parameter(request, "artifact_version")
    unavailable = _workspace_error_response(
        404,
        {"code": "scope_invalid", "message": "Workspace is unavailable for this scope."},
    )
    if version is None:
        return unavailable
    prefix = dossier_store.STAGING_PREFIX
    try:
        bucket = _investigation_bucket()
        artifact = dossier_artifacts.read_artifact(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            artifact_id=artifact_id,
        )
        if (
            artifact is None
            or artifact["manifest"]["scope_digest"] != scope.scope_digest
            or artifact["artifact_version"] != version
            or artifact["contract_version"] != dossier_artifacts.ARTIFACT_CONTRACT_VERSION
            or dossier_artifacts.PDF_EXPORT not in artifact["manifest"]["export_digests"]
        ):
            return unavailable
        if not _approved_export_decision(bucket=bucket, prefix=prefix, artifact=artifact):
            return _workspace_error_response(
                409,
                {
                    "code": "artifact_approval_required",
                    "message": "Exact artifact review is required.",
                },
            )
        if extension == "html":
            data = artifact["html"].encode("utf-8")
        else:
            data = dossier_artifacts.read_artifact_pdf(
                bucket=bucket, prefix=prefix, artifact=artifact
            )
            if data is None:
                raise dossier_artifacts.ArtifactInvalid("pdf export is missing")
    except (
        deployment_contract.DeploymentContractError,
        dossier_store.DossierStoreError,
        dossier_artifacts.ArtifactInvalid,
    ):
        return _workspace_error_response(
            503, {"code": "workspace_unavailable", "message": "Workspace is unavailable."}
        )
    return Response(
        data,
        media_type=_EXPORT_MEDIA_TYPES[extension],
        headers={
            **_WORKSPACE_HEADERS,
            "Content-Disposition": (
                f'attachment; filename="42-client-read-{artifact["artifact_id"]}.{extension}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


def _committed_artifact_state(*, bucket, prefix: str, artifact: dict) -> str:
    """The committed review state of an artifact, approved only when exactly bound."""
    pointer = dossier_review_store.read_state_pointer(
        bucket=bucket,
        prefix=prefix,
        investigation_id=artifact["investigation_id"],
        dossier_version=artifact["dossier_version"],
    )["pointer"]
    state = dossier_review_store.resource_pointer_state(
        pointer, resource="artifact", resource_id=artifact["artifact_id"]
    )["state"]
    if state == "approved" and not _approved_export_decision(
        bucket=bucket, prefix=prefix, artifact=artifact
    ):
        return "pending_review"
    return state


@app.get(
    "/api/internal/v2/investigations/{investigation_id}/dossier/working",
    include_in_schema=False,
)
async def api_dossier_working(
    investigation_id: str, request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """The internal working view of the current or one exact dossier version.

    The resolver's working response, plus the exact content digest of every
    reviewable claim and relationship, which is the resource_version the
    review command checks, and every artifact prepared for this investigation
    under this scope with its committed state. The one optional query
    parameter is dossier_version. A version this investigation does not hold
    is refused as an unavailable scope, which says nothing about any other
    investigation or version.

    The view carries pending and rejected material and publication records,
    so it needs a bound review session and a contract role, like the preview;
    the passcode alone reads nothing here.
    """
    try:
        principal = _review_session_principal(request)
        if principal.get("role") not in dossier_approval.APPROVAL_ROLES:
            raise _ReviewRefused(
                403, dossier_review_auth.ROLE_REQUIRED, "principal holds no contract role"
            )
        params = request.query_params.multi_items()
        if [key for key, _value in params] not in ([], ["dossier_version"]):
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "working request is invalid"
            )
        version = params[0][1] if params else None
        if version is not None and not dossier_store._digest(version):
            raise _ReviewRefused(
                400, dossier_review_auth.REQUEST_INVALID, "dossier version is invalid"
            )
        scope = _review_scope(investigation_id)
        bucket = _review_bucket()
        prefix = dossier_store.STAGING_PREFIX
        resolution = dossier_resolver.resolve_dossier(scope, version, bucket=bucket)
        if resolution["state"] != "ready":
            if resolution["reason"] == "dossier_storage_unavailable":
                raise _ReviewRefused(
                    503, "review_storage_unavailable", "review storage failed"
                )
            raise _ReviewRefused(404, "scope_invalid", "scope is unavailable")
        try:
            state = dossier_review_store.read_state_pointer(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                dossier_version=resolution["dossier_version"],
            )
            decisions = dossier_review_store.list_decisions(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                dossier_version=resolution["dossier_version"],
            )
            applied = dossier_review_store.applied_decisions(decisions, state["pointer"])
            payload = dossier_resolver.working_response(resolution, applied)
            payload["resource_versions"] = dossier_resolver.resource_versions(
                resolution["dossier"]
            )
            payload["artifacts"] = [
                {
                    "artifact_id": artifact["artifact_id"],
                    "artifact_version": artifact["artifact_version"],
                    "dossier_version": artifact["dossier_version"],
                    "state": _committed_artifact_state(
                        bucket=bucket, prefix=prefix, artifact=artifact
                    ),
                }
                for artifact in dossier_artifacts.list_artifacts(
                    bucket=bucket, prefix=prefix, investigation_id=scope.investigation_id
                )
                if artifact["manifest"]["scope_digest"] == scope.scope_digest
            ]
        except (dossier_store.DossierStoreError, dossier_artifacts.ArtifactInvalid):
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    return _workspace_response(payload)


@app.post("/api/v2/investigations/{investigation_id}/artifacts/{artifact_id}/export.html")
async def api_investigation_artifact_export_html(
    investigation_id: str,
    artifact_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """The approved artifact's stored standalone HTML, at one exact version."""
    return await _artifact_export(request, investigation_id, artifact_id, "html")


@app.post("/api/v2/investigations/{investigation_id}/artifacts/{artifact_id}/export.pdf")
async def api_investigation_artifact_export_pdf(
    investigation_id: str,
    artifact_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """The approved artifact's stored PDF, made from that same HTML, at one exact version."""
    return await _artifact_export(request, investigation_id, artifact_id, "pdf")


@app.post(
    "/api/internal/v2/investigations/{investigation_id}/artifacts/{artifact_id}/preview",
    include_in_schema=False,
)
async def api_artifact_preview(
    investigation_id: str,
    artifact_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """Read exact pending bytes for review, and record the preview on the timeline."""
    response = await _artifact_preview(investigation_id, artifact_id, request)
    _emit_artifact_outcome(
        response,
        stage="artifact_review",
        action="preview",
        investigation_id=investigation_id,
        artifact_id=artifact_id,
        format="html",
    )
    return response


async def _artifact_preview(
    investigation_id: str, artifact_id: str, request: Request
) -> Response:
    """Read exact pending narrative bytes for an authenticated artifact reviewer."""
    try:
        principal = _review_principal(request)
        body = await _review_json(request)
        if (
            type(body) is not dict
            or set(body) != {"contract_version", "artifact_version"}
            or body["contract_version"] != "dossier_artifact_preview_v1"
            or not dossier_store._digest(body["artifact_version"])
        ):
            raise _ReviewRefused(
                400, "narrative_request_invalid", "narrative request is invalid"
            )
        if not {"dossier_editor", "client_read_approver"} & set(_principal_roles(principal)):
            raise _ReviewRefused(
                403, dossier_review_auth.ROLE_REQUIRED, "role cannot preview an artifact"
            )
        try:
            dossier_artifacts.artifact_object_name(
                dossier_store.STAGING_PREFIX, investigation_id, artifact_id
            )
        except ValueError:
            raise _ReviewRefused(
                404, "scope_invalid", "scope is unavailable"
            ) from None
        scope = _review_scope(investigation_id)
        bucket = _review_bucket()
        prefix = dossier_store.STAGING_PREFIX
        try:
            artifact = dossier_artifacts.read_artifact(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                artifact_id=artifact_id,
            )
            if (
                artifact is None
                or artifact["contract_version"]
                != dossier_artifacts.ARTIFACT_CONTRACT_VERSION_V2
                or artifact["content_kind"]
                != dossier_artifacts.NARRATIVE_CONTENT_KIND
                or artifact["format"] != "html"
                or artifact["manifest"]["scope_digest"] != scope.scope_digest
                or artifact["artifact_version"] != body["artifact_version"]
            ):
                raise _ReviewRefused(
                    404, "scope_invalid", "scope is unavailable"
                )
            current = dossier_store.read_pointer(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
            )
            if (
                current is not None
                and current["pointer"]["scope_digest"] != scope.scope_digest
            ):
                raise _ReviewRefused(
                    404, "scope_invalid", "scope is unavailable"
                )
            if current is None or (
                current["pointer"]["dossier_version"]
                != artifact["dossier_version"]
            ):
                raise _ReviewRefused(
                    409,
                    "review_version_conflict",
                    "dossier version is not current",
                )
            pointer = dossier_review_store.read_state_pointer(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
                dossier_version=artifact["dossier_version"],
            )["pointer"]
            entry, _approved = _validated_v2_artifact_review(
                bucket=bucket,
                prefix=prefix,
                scope=scope,
                artifact=artifact,
                pointer=pointer,
            )
        except _ReviewRefused:
            raise
        except (
            dossier_store.DossierStoreError,
            dossier_artifacts.ArtifactInvalid,
        ):
            raise _ReviewRefused(
                503, "review_storage_unavailable", "review storage failed"
            ) from None
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    return _workspace_response(
        {
            "contract_version": "dossier_artifact_preview_v1",
            "investigation_id": scope.investigation_id,
            "dossier_version": artifact["dossier_version"],
            "artifact_id": artifact["artifact_id"],
            "artifact_version": artifact["artifact_version"],
            "format": "html",
            "content_kind": dossier_artifacts.NARRATIVE_CONTENT_KIND,
            "state": entry["state"],
            "manifest": artifact["manifest"],
            "client_read": artifact["client_read"],
            "html": artifact["html"],
            "delivery": "review_preview",
        }
    )


def _research_row_in_scope(row: object) -> bool:
    """A research row is served only when the scope it stores is the server's own."""
    try:
        server_scope = investigation_scopes.default_client_scope_id()
    except investigation_scopes.InvestigationScopeInvalid:
        return False
    return isinstance(row, dict) and row.get("client_scope_id") == server_scope


def _research_artifact_row(artifact_id: object) -> dict:
    """One research artifact bound to the server scope, or a scope refusal.

    The id must fit the persisted grammar before anything is read, the server
    scope comes from the configured default and never from the caller, and the
    row is served only when the scope it stores is that one. A row without a
    stored scope is not bound to anything and is refused the same way.

    Deployment note: research rows written before the scope was stamped carry
    no client_scope_id and are unreadable through every research route until
    they are backfilled by a separate approved operation.
    """
    if type(artifact_id) is not str or _RESEARCH_ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise _ReviewRefused(404, "scope_invalid", "Workspace is unavailable for this scope.")
    row = bq.get_research_artifact(artifact_id)
    if not _research_row_in_scope(row):
        raise _ReviewRefused(404, "scope_invalid", "Workspace is unavailable for this scope.")
    return row


@app.post("/api/v2/investigations/{investigation_id}/status")
async def api_investigation_status(
    investigation_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    scope = await _resolved_workspace_request(request, investigation_id)
    return _workspace_response(workspace_reads.status_resource(scope))


@app.post("/api/v2/investigations/{investigation_id}/claims/read")
async def api_investigation_claims(
    investigation_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    scope = await _resolved_workspace_request(request, investigation_id)
    return _workspace_response(workspace_reads.claims_resource(scope))


@app.post("/api/v2/investigations/{investigation_id}/decision/read")
async def api_investigation_decision(
    investigation_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    scope = await _resolved_workspace_request(request, investigation_id)
    return _workspace_response(workspace_reads.decision_resource(scope))


@app.post("/api/v2/investigations/{investigation_id}/artifacts/{artifact_id}/read")
async def api_investigation_artifact(
    investigation_id: str,
    artifact_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """The approved artifact read, recorded on the timeline as its export."""
    try:
        response = await _investigation_artifact(investigation_id, artifact_id, request)
    except HTTPException as error:
        detail = error.detail if type(error.detail) is dict else {}
        _emit_artifact_outcome(
            JSONResponse({"detail": detail}, status_code=error.status_code),
            stage="artifact_export",
            action="read",
            investigation_id=investigation_id,
            artifact_id=artifact_id,
        )
        raise
    _emit_artifact_outcome(
        response,
        stage="artifact_export",
        action="read",
        investigation_id=investigation_id,
        artifact_id=artifact_id,
    )
    return response


async def _investigation_artifact(
    investigation_id: str, artifact_id: str, request: Request
) -> Response:
    """One artifact at one exact version, for the scope the server resolved.

    The grammar is the child-read grammar plus exactly one query parameter,
    artifact_version, which must be the version stored. Anything else about
    the artifact, absent, another scope, another version, is refused with the
    same code, so a refusal reveals nothing about what exists. The state is the
    committed review state, approved only through the exact-version predicate.
    """
    scope = await _resolved_workspace_request(request, investigation_id)
    version = _single_query_parameter(request, "artifact_version")
    unavailable = _workspace_error_response(
        404,
        {"code": "scope_invalid", "message": "Workspace is unavailable for this scope."},
    )
    if version is None:
        return unavailable
    prefix = dossier_store.STAGING_PREFIX
    try:
        bucket = _investigation_bucket()
        artifact = dossier_artifacts.read_artifact(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            artifact_id=artifact_id,
        )
        if (
            artifact is None
            or artifact["manifest"]["scope_digest"] != scope.scope_digest
            or artifact["artifact_version"] != version
        ):
            return unavailable
        if (
            artifact["contract_version"]
            == dossier_artifacts.ARTIFACT_CONTRACT_VERSION_V2
        ):
            current = dossier_store.read_pointer(
                bucket=bucket,
                prefix=prefix,
                investigation_id=scope.investigation_id,
            )
            if (
                current is not None
                and current["pointer"]["scope_digest"] != scope.scope_digest
            ):
                return unavailable
            if (
                current is None
                or current["pointer"]["dossier_version"]
                != artifact["dossier_version"]
            ):
                return _workspace_error_response(
                    409,
                    {
                        "code": "review_version_conflict",
                        "message": "dossier version is not current",
                    },
                )
        pointer = dossier_review_store.read_state_pointer(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            dossier_version=artifact["dossier_version"],
        )["pointer"]
        if (
            artifact["contract_version"]
            == dossier_artifacts.ARTIFACT_CONTRACT_VERSION_V2
        ):
            entry, approved = _validated_v2_artifact_review(
                bucket=bucket,
                prefix=prefix,
                scope=scope,
                artifact=artifact,
                pointer=pointer,
            )
            state = entry["state"]
            if state != "approved" or not approved:
                return _workspace_error_response(
                    409,
                    {
                        "code": "artifact_approval_required",
                        "message": "Exact artifact review is required.",
                    },
                )
        else:
            entry = dossier_review_store.resource_pointer_state(
                pointer, resource="artifact", resource_id=artifact_id
            )
            state = entry["state"]
            if entry["applied"]:
                decision = dossier_review_store.read_decision(
                    bucket=bucket,
                    prefix=prefix,
                    investigation_id=scope.investigation_id,
                    dossier_version=artifact["dossier_version"],
                    decision_id=entry["applied"][-1],
                )
                approved = dossier_artifacts.is_artifact_approved(
                    manifest=artifact["manifest"],
                    decision=_artifact_decision_binding(
                        decision, artifact["manifest"]
                    ),
                )
                if state == "approved" and not approved:
                    state = "pending_review"
        pdf = None
        if (
            state == "approved"
            and artifact["contract_version"] == dossier_artifacts.ARTIFACT_CONTRACT_VERSION
            and dossier_artifacts.PDF_EXPORT in artifact["manifest"]["export_digests"]
        ):
            # The exact stored PDF travels with the approved read, checked
            # against the digest the approval bound.
            pdf = dossier_artifacts.read_artifact_pdf(
                bucket=bucket, prefix=prefix, artifact=artifact
            )
            if pdf is None:
                raise dossier_artifacts.ArtifactInvalid("pdf export is missing")
    except (
        deployment_contract.DeploymentContractError,
        dossier_store.DossierStoreError,
        dossier_artifacts.ArtifactInvalid,
    ):
        return _workspace_error_response(
            503, {"code": "workspace_unavailable", "message": "Workspace is unavailable."}
        )
    payload = {
        "contract_version": artifact["contract_version"],
        "investigation_id": scope.investigation_id,
        "dossier_version": artifact["dossier_version"],
        "artifact_id": artifact["artifact_id"],
        "artifact_version": artifact["artifact_version"],
        "format": artifact["format"],
    }
    if artifact["contract_version"] == dossier_artifacts.ARTIFACT_CONTRACT_VERSION_V2:
        payload["content_kind"] = artifact["content_kind"]
    payload.update(
        state=state,
        manifest=artifact["manifest"],
        client_read=artifact["client_read"],
        html=artifact["html"],
    )
    if pdf is not None:
        payload["pdf"] = base64.b64encode(pdf).decode("ascii")
    return _workspace_response(payload)


@app.post(
    "/api/internal/v2/investigations/{investigation_id}/historical/read",
    include_in_schema=False,
)
async def api_historical_workspace(
    investigation_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    try:
        raw = await request.json()
    except Exception:
        return _workspace_error_response(
            400,
            {
                "code": "workspace_request_invalid",
                "message": "Workspace request is invalid.",
            },
        )
    if (
        not isinstance(raw, dict)
        or tuple(raw) != ("mode",)
        or raw.get("mode") not in historical_workspace.MODES
    ):
        return _workspace_error_response(
            400,
            {
                "code": "workspace_request_invalid",
                "message": "Workspace request is invalid.",
            },
        )
    try:
        payload = historical_workspace.read_historical_workspace(
            investigation_id, raw["mode"]
        )
    except workspace_scope.WorkspaceScopeError as error:
        return _workspace_error_response(error.status_code, error.detail)
    if isinstance(payload.get("detail"), dict):
        return _workspace_error_response(503, payload["detail"])
    return _workspace_response(payload)


# Fieldwork scope is resolved by the server. There is no caller selector for
# it, so it is pinned here rather than derived from a request.
FIELDWORK_CLIENT_SCOPE_ID = "ogilvy_default"
FIELDWORK_MARKET_SCOPE = ("za", "ng", "ke")


def _single_query_parameter(request: Request, name: str) -> str | None:
    """The stripped value of exactly one query parameter, or None for any other grammar."""
    params = request.query_params.multi_items()
    if [key for key, _ in params] != [name]:
        return None
    value = params[0][1].strip()
    return value or None


def _index_request_error() -> JSONResponse:
    return _workspace_error_response(
        400,
        {
            "code": "workspace_request_invalid",
            "message": "Workspace request is invalid.",
        },
    )


def _index_version_or_error(request: Request) -> JSONResponse | None:
    """The exact request grammar, and nothing else.

    One parameter, named once, non blank, matching the supported version. Scope
    is server owned, so a caller supplying its own is asking to widen its view,
    which is refused as an invalid request rather than honoured.
    """
    version = _single_query_parameter(request, "contract_version")
    if version is None:
        return _index_request_error()
    if version != investigation_index.CONTRACT_VERSION:
        return _workspace_error_response(
            400,
            {
                "code": "contract_version_unsupported",
                "message": "Workspace contract version is unsupported.",
            },
        )
    return None


def _latest_released_run_id(scope_id: str) -> str | None:
    """The one released run an investigation may be framed against, or None.

    Reuses the exact qualifying-receipt reader Open Discover uses, so framing
    and discovery can never disagree about whether a run is released. Every
    failure direction collapses to None, which the framing surface reads as
    refuse-to-create: a broken lookup can suppress framing, never enable it.
    """
    try:
        rows = bq.fetch_dynamic_run_receipt("all")
    except Exception:
        return None
    if not rows:
        return None
    run_id = rows[0].get("run_id") if isinstance(rows[0], dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


@app.post("/api/v2/investigations/scopes", include_in_schema=False)
def api_investigation_scopes_read(
    request: Request,
    _gate: None = Depends(require_passcode),
) -> JSONResponse:
    """Which scopes exist, and whether a run has been released against each.

    The passcode dependency runs first, so an unauthenticated caller never
    reaches the configuration.
    """
    refusal = _index_version_or_error(request)
    if refusal is not None:
        return refusal
    return _workspace_response(
        investigation_index.read_scopes(latest_run_lookup=_latest_released_run_id)
    )


@app.post("/api/v2/investigations/list", include_in_schema=False)
def api_investigation_index_read(
    request: Request,
    _gate: None = Depends(require_passcode),
) -> JSONResponse:
    """The investigations in scope, most recent first and bounded.

    Records come from the server's own scope. Until durable listing is wired to
    storage this returns an honest empty list rather than a fixture: an empty
    estate is a fact, and inventing rows to fill a page is the one thing this
    surface exists to prevent.
    """
    refusal = _index_version_or_error(request)
    if refusal is not None:
        return refusal
    try:
        records = investigation_index.read_stored_investigations(
            bucket=_investigation_bucket()
        )
    except Exception:
        # Unavailable is a different fact from empty. An estate that could not
        # be read must never render as an estate with nothing in it.
        return _workspace_error_response(
            503,
            {
                "code": "workspace_unavailable",
                "message": "Investigations are unavailable.",
            },
        )
    # Scope is server owned and pinned here. Without it the index filter is
    # dead code, and a second enabled scope would leak into this desk's view.
    return _workspace_response(
        investigation_index.read_index(
            records, client_scope_id=FIELDWORK_CLIENT_SCOPE_ID
        )
    )


@app.get(
    "/api/internal/v2/investigations/{investigation_id}/dossier/read",
    include_in_schema=False,
)
def api_intelligence_dossier_read(
    investigation_id: str,
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """One investigation's dossier, projected server side.

    The passcode dependency runs first, so an unauthenticated caller never
    reaches storage. The grammar takes one parameter and nothing else: a caller
    must not be able to widen its own scope, and in particular must not be able
    to ask for the material the projection exists to withhold.
    """
    version = _single_query_parameter(request, "contract_version")
    if version is None:
        return _workspace_error_response(
            400,
            {
                "code": "workspace_request_invalid",
                "message": "Workspace request is invalid.",
            },
        )
    if version != intelligence_dossier.CONTRACT_VERSION:
        return _workspace_error_response(
            400,
            {
                "code": "contract_version_unsupported",
                "message": "Workspace contract version is unsupported.",
            },
        )
    try:
        scope = workspace_scope.resolve_workspace_scope(investigation_id)
    except workspace_scope.WorkspaceScopeError as error:
        return _workspace_error_response(error.status_code, error.detail)
    try:
        bucket = _investigation_bucket()
    except deployment_contract.DeploymentContractError:
        return _dossier_read_unavailable()
    # The scoped current pointer, then the exact immutable version it names.
    # Absence is a named state, never an empty record that would read as ready.
    resolution = dossier_resolver.resolve_dossier(scope, dossier_version=None, bucket=bucket)
    if resolution["state"] != "ready":
        return _dossier_read_refused(resolution["reason"])
    prefix = dossier_store.STAGING_PREFIX
    try:
        state = dossier_review_store.read_state_pointer(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            dossier_version=resolution["dossier_version"],
        )
        decisions = dossier_review_store.list_decisions(
            bucket=bucket,
            prefix=prefix,
            investigation_id=scope.investigation_id,
            dossier_version=resolution["dossier_version"],
        )
        applied = dossier_review_store.applied_decisions(decisions, state["pointer"])
        payload = intelligence_dossier.read_dossier(
            investigation_id,
            dossier_resolver.projection_record(resolution["dossier"], applied),
        )
    except (dossier_store.DossierStoreError, intelligence_dossier.DossierContractError):
        return _dossier_read_unavailable()
    return _workspace_response(payload)


def _dossier_read_unavailable() -> Response:
    return _workspace_error_response(
        503,
        {"code": "workspace_unavailable", "message": "Workspace is unavailable."},
    )


def _dossier_read_refused(reason: str) -> Response:
    """The resolver's named absence, mapped onto the workspace error grammar."""
    if reason == "dossier_storage_unavailable":
        return _dossier_read_unavailable()
    if reason == "dossier_scope_mismatch":
        return _workspace_error_response(
            404,
            {"code": "scope_invalid", "message": "Workspace is unavailable for this scope."},
        )
    if reason == "dossier_request_invalid":
        return _workspace_error_response(
            400,
            {"code": "workspace_request_invalid", "message": "Workspace request is invalid."},
        )
    return _workspace_error_response(
        404,
        {
            "code": "dossier_unavailable",
            "reason": reason,
            "message": "No dossier is published for this investigation.",
        },
    )


@app.get("/api/internal/v2/fieldwork/read", include_in_schema=False)
async def api_fieldwork_read(
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    """The Fieldwork operations dossier for the server-resolved scope.

    The passcode dependency above runs first, so an unauthenticated caller
    never reaches storage or Source Lab. The query grammar is exact and takes
    no client, market, vendor, source, budget, investigation, actor or
    execution selector: scope is the server's, not the caller's.
    """
    version = _single_query_parameter(request, "contract_version")
    if version is None:
        return _workspace_error_response(
            400,
            {
                "code": "workspace_request_invalid",
                "message": "Workspace request is invalid.",
            },
        )
    if version not in (fieldwork.CONTRACT_VERSION, fieldwork.CONTRACT_VERSION_V2):
        return _workspace_error_response(
            400,
            {
                "code": "contract_version_unsupported",
                "message": "Workspace contract version is unsupported.",
            },
        )
    try:
        if version == fieldwork.CONTRACT_VERSION_V2:
            scope = general_question_routes._current_scope()
            observations = await asyncio.gather(
                general_question_routes.read_question_observation(request, None),
                asyncio.to_thread(
                    lambda: investigation_index.observe_fieldwork_investigations(
                        bucket=_investigation_bucket(),
                        scope=scope,
                    )
                ),
                return_exceptions=True,
            )
            for index, observed in enumerate(observations):
                if isinstance(observed, BaseException):
                    coverage = fieldwork._unavailable_coverage_v2()
                    coverage["reasons"] = ["listing_unavailable"]
                    observations[index] = {
                        "scope": scope,
                        "coverage": coverage,
                        "rows": [],
                    }
            payload = fieldwork.build_fieldwork_workspace_v2(
                scope=scope,
                general_questions=observations[0],
                investigation_plans=observations[1],
            )
        else:
            payload = fieldwork.read_fieldwork_workspace(
                client_scope_id=FIELDWORK_CLIENT_SCOPE_ID,
                market_scope=FIELDWORK_MARKET_SCOPE,
            )
    except (
        fieldwork.FieldworkContractError,
        investigation_scopes.InvestigationScopeInvalid,
    ):
        return _workspace_error_response(
            503,
            {"code": "workspace_unavailable", "message": "Workspace is unavailable."},
        )
    return _workspace_response(payload)


@app.get("/api/internal/v2/fieldwork/question", include_in_schema=False)
async def api_fieldwork_question(
    request: Request,
    _gate: None = Depends(require_passcode),
) -> Response:
    from src.api.question_worker_process import WorkerProcessError
    from src.api.question_worker_protocol import _uuid

    params = request.query_params.multi_items()
    if len(params) != 2 or {key for key, _ in params} != {
        "contract_version",
        "request_id",
    }:
        return _index_request_error()
    values = dict(params)
    if values["contract_version"] != "general_question_detail_v1" or not _uuid(
        values["request_id"]
    ):
        return _index_request_error()
    try:
        payload = await general_question_routes.read_question_observation(
            request, values["request_id"]
        )
    except WorkerProcessError as error:
        if error.reason == "observation_request_unavailable":
            return _workspace_error_response(
                404,
                {"code": "request_unavailable", "message": "Request is unavailable."},
            )
        return _workspace_error_response(
            503,
            {"code": "workspace_unavailable", "message": "Workspace is unavailable."},
        )
    except Exception:
        return _workspace_error_response(
            503,
            {"code": "workspace_unavailable", "message": "Workspace is unavailable."},
        )
    return _workspace_response(payload)


@app.get("/api/today")
def api_today(market: str = "all", _gate: None = Depends(require_passcode)) -> dict:
    _validate_market(market)
    return _market_cached("today", market, lambda: bq.fetch_today(market))


@app.get("/api/metrics")
def api_metrics(_gate: None = Depends(require_passcode)) -> dict:
    return _metrics_cached(lambda: bq.fetch_growth_metrics())


@app.get("/api/seeds")
def api_seeds(_gate: None = Depends(require_passcode)) -> dict:
    return _market_cached(
        "seeds",
        "all",
        seeds.build_seeds_payload,
        cacheable=lambda payload: bool(payload.get("seeds")),
    )


@app.get("/api/seed-path")
def api_seed_path(
    keyword: str = Query(default=""),
    market: str = "za",
    _gate: None = Depends(require_passcode),
) -> dict:
    mk = market.strip().lower()
    if mk not in SINGLE_MARKETS:
        raise HTTPException(status_code=400, detail="market must be za, ng, or ke")
    kw = bq.normalize_query(keyword)
    if not kw:
        raise HTTPException(status_code=400, detail="keyword is required")
    # A BQ hiccup degrades to a 503 the client can retry, matching the other
    # data endpoints, instead of a bare 500 traceback.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            adj_future = pool.submit(bq.fetch_seed_graph_adjacency, kw, mk)
            path_future = pool.submit(bq.fetch_seed_path, kw, mk)
            adjacency = adj_future.result()
            path = path_future.result()
    except Exception:
        logger.warning("seed-path read failed for %s/%s", kw, mk, exc_info=True)
        raise HTTPException(status_code=503, detail="Seed path not available yet.")
    return {"keyword": kw, "market": mk, "adjacency": adjacency, "path": path}


@app.get("/api/seed-discover")
def api_seed_discover(
    market: str = Query(default=""),
    status: str = Query(default="pending"),
    _gate: None = Depends(require_passcode),
) -> dict:
    mk = market.strip().lower()
    if mk and mk not in SINGLE_MARKETS:
        raise HTTPException(status_code=400, detail="market must be za, ng, or ke")
    st = (status or "pending").strip().lower()
    if st not in ("pending", "decided", "week"):
        raise HTTPException(
            status_code=400, detail="status must be pending, decided, or week"
        )
    cache_key = f"{mk or 'all'}:{st}"

    def load():
        return seed_discover.build_discover_payload(market=mk or None, status=st)

    # An error payload (BQ read failed) must not be pinned for the whole TTL.
    return _market_cached(
        "seed-discover", cache_key, load, cacheable=lambda p: "error" not in p
    )


@app.get("/api/market-topics")
def api_market_topics(
    markets: str = Query(default="za,ng,ke"),
    per_market: int = Query(default=10),
    _gate: None = Depends(require_passcode),
) -> dict:
    mks = [m.strip().lower() for m in (markets or "").split(",") if m.strip()]
    for mk in mks:
        if mk not in SINGLE_MARKETS:
            raise HTTPException(status_code=400, detail="market must be za, ng, or ke")
    if not mks:
        mks = list(SINGLE_MARKETS)
    per = max(1, min(int(per_market or 10), 25))
    rows = bq.fetch_market_topics(mks, per_market=per)
    for r in rows:
        r["label"] = bq.topic_label(str(r.get("query_group") or ""))
    return {"topics": rows, "markets": mks}


@app.get("/api/intel/mentions")
def api_intel_mentions(
    response: Response,
    market: str = "za",
    sentiment: str = None,
    category: str = None,
    cursor: str = None,
    _gate: None = Depends(require_passcode),
) -> dict:
    # Sourced from the engine's enriched_content (real post text per market).
    # A live feed must never be served stale from a browser or proxy cache.
    response.headers["Cache-Control"] = "no-store"
    mk = (market or "").strip().lower()
    if mk not in ("za", "ng", "ke", "all"):
        raise HTTPException(status_code=400, detail="market must be za, ng, ke, or all")
    return bq.listen_feed(mk, sentiment=sentiment, category=category)


@app.get("/api/voices")
def api_voices(
    request: Request, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    """Reach-ranked board of named voices for a region."""
    _check_rate_limit(request)
    _validate_market(region)
    return _market_cached("voices", region, lambda: creator.build_voices(region))


@app.get("/api/creator/{handle}")
def api_creator(
    request: Request, handle: str, _gate: None = Depends(require_passcode)
) -> dict:
    """One creator's 30-day profile. 404 when the handle has no window rows."""
    _check_rate_limit(request)
    profile = _market_cached(
        "creator", handle.lower(), lambda: creator.build_creator_profile(handle)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No such creator in the window")
    return profile


@app.get("/api/creator/{handle}/reach-series")
def api_creator_reach_series(
    handle: str, _gate: None = Depends(require_passcode)
) -> dict:
    """The creator's daily reach series, also embedded in the profile payload."""
    profile = _market_cached(
        "creator", handle.lower(), lambda: creator.build_creator_profile(handle)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No such creator in the window")
    return {"series": profile.get("reach_series", [])}


@app.get("/api/creator/{handle}/posts")
def api_creator_posts(handle: str, _gate: None = Depends(require_passcode)) -> dict:
    """The creator's post wall, newest first, also embedded in the profile."""
    profile = _market_cached(
        "creator", handle.lower(), lambda: creator.build_creator_profile(handle)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No such creator in the window")
    return {"posts": profile.get("wall", []), "count": profile.get("wall_count", 0)}


def _topic_profile(topic_id: str, region: str):
    """The cached topic story for one region, desk brief merged with aggregates.

    Resolves the desk brief (base) for the topic. If it is not on the requested
    region's desk, falls back to the ALL desk and the topic's own market, so a
    topic live in any market still loads with its story instead of 404ing. This
    is what lets a cross-market evidence link (e.g. ke/food_nyamachoma opened
    from the ZA desk) reach the topic rather than dead-end on 'No signal'."""
    desk_payload = _desk_cached(region, lambda: desk.build_desk_payload(region))
    base = next(
        (t for t in desk_payload.get("topics", []) if t.get("id") == topic_id), None
    )
    if base is None and region != "all":
        all_payload = _desk_cached("all", lambda: desk.build_desk_payload("all"))
        base = next(
            (t for t in all_payload.get("topics", []) if t.get("id") == topic_id), None
        )
        if base is not None:
            region = str(base.get("region") or region).lower()
    return _market_cached(
        "topic",
        region + ":" + topic_id,
        lambda: topic.build_topic_profile(topic_id, region, base),
    )


@app.get("/api/topic/{topic_id}")
def api_topic(
    request: Request,
    topic_id: str,
    region: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    """One topic's full story: the desk brief merged with live aggregates."""
    _check_rate_limit(request)
    _validate_market(region)
    profile = _topic_profile(topic_id, region)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such topic in the window")
    return profile


@app.get("/api/topic/{topic_id}/sov-series")
def api_topic_sov_series(
    topic_id: str, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    """The topic's daily share-of-voice series, also embedded in the story."""
    _validate_market(region)
    profile = _topic_profile(topic_id, region)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such topic in the window")
    return {"series": profile.get("sov_series", [])}


@app.get("/api/topic/{topic_id}/momentum-series")
def api_topic_momentum_series(
    topic_id: str, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    """The topic's daily observed-volume series, also embedded in the story."""
    _validate_market(region)
    profile = _topic_profile(topic_id, region)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such topic in the window")
    return {"series": profile.get("volume_series", [])}


@app.get("/api/lexicon/{term}")
def api_lexicon_term(
    term: str, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    """The posts a slang term is carried in, newest first."""
    _validate_market(region)
    return _market_cached(
        "lexicon_term",
        region + ":" + term.lower(),
        lambda: lexicon.build_lexicon_term(term, region),
    )


def _build_listen(query: str, region: str) -> dict:
    """Assemble the Campaign Listening payload: per-channel share of voice, a
    term-level media tone, and the matched voice wall. Reuses the relevance-
    ranked search retrieval; no Vertex synthesis."""
    market = region.strip().lower()
    raw = bq.search_content(query, market)
    items = bq.clean_items(raw.get("items") or [])
    wall = []
    for p in items[:24]:
        wall.append(
            {
                "id": p.get("id"),
                "text": p.get("text"),
                "platform": bq._platform_label(p.get("platform")),
                "market": (p.get("market") or "").upper(),
                "engagement": int(p.get("engagement") or 0),
                "age": bq.age_label(bq.best_stamp(p)),
                "handle": p.get("handle") or "",
                "url": p.get("url") or "",
            }
        )
    sig = bq.fetch_listen(query, market)
    return {
        "query": query,
        "market": market,
        "total_matches": int(raw.get("total_matches") or 0),
        "sov_by_channel": sig.get("sov_by_channel") or [],
        "media_tone": sig.get("media_tone"),
        "tone_n": sig.get("tone_n") or 0,
        "entities": bq.top_entities(items),
        "wall": wall,
        "wall_count": len(wall),
    }


@app.get("/api/listen")
def api_listen(
    request: Request,
    q: str,
    region: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    """Ad-hoc campaign or topic listening: share of voice by channel, the voice
    wall, and a term-level media tone. No model synthesis."""
    _check_rate_limit(request)
    _validate_market(region)
    query = _bounded_search_text((q or "").strip(), "The search term")
    if not query:
        raise HTTPException(status_code=400, detail="A search term is required")
    return _market_cached(
        "listen", region + ":" + query.lower(), lambda: _build_listen(query, region)
    )


@app.get("/api/rails")
def api_rails(market: str = "all", _gate: None = Depends(require_passcode)) -> dict:
    _validate_market(market)
    return _market_cached("rails", market, lambda: bq.fetch_rails(market))


@app.get("/api/desk")
def api_desk(
    request: Request, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    _check_rate_limit(request)
    _validate_market(region)
    return _desk_cached(region, lambda: desk.build_desk_payload(region))


@app.get("/api/desk/topic/{topic_id}")
def api_desk_topic(
    request: Request,
    topic_id: str,
    region: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    _check_rate_limit(request)
    _validate_market(region)
    payload = _desk_cached(region, lambda: desk.build_desk_payload(region))
    for t in payload.get("topics", []):
        if t.get("id") == topic_id:
            return t
    raise HTTPException(status_code=404, detail="No such topic on the desk")


@app.get("/api/desk/tone-split")
def api_desk_tone_split(
    request: Request, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    _check_rate_limit(request)
    """The day's positive/neutral/negative tone split for a region.

    Reads off the cached desk payload so it shares the desk TTL and fires no
    extra query. Empty ({}) until the engine writes sentiment_lexicon_score and
    the Wave 2 migration applies, so the client renders nothing today."""
    _validate_market(region)
    payload = _desk_cached(region, lambda: desk.build_desk_payload(region))
    return {"region": region, "tone_split": payload.get("tone_split") or {}}


@app.get("/api/desk/pan-african")
def api_desk_pan_african(
    request: Request, region: str = "all", _gate: None = Depends(require_passcode)
) -> dict:
    _check_rate_limit(request)
    """The pan-African cross-market stories for a region.

    Reads off the cached desk payload so it shares the desk TTL and fires no
    extra query. status is the surface's own state: ready, no_stories, or
    unavailable with the error that says why, so an absent table is never
    served as a day without stories."""
    _validate_market(region)
    payload = _desk_cached(region, lambda: desk.build_desk_payload(region))
    surface = payload.get("pan_african_surface") or {}
    return {
        "region": region,
        "status": surface.get("status"),
        "error": surface.get("error"),
        "stories": payload.get("pan_african") or [],
    }


@app.get("/api/desk/opportunity/{topic_id}")
def api_desk_opportunity(
    request: Request,
    topic_id: str,
    region: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    """Read a still-fresh cached opportunity without starting generation."""
    _check_rate_limit(request)
    _validate_market(region)
    payload = _desk_cached(region, lambda: desk.build_desk_payload(region))
    for t in payload.get("topics", []):
        if t.get("id") == topic_id:
            opportunity = synth.cache_get(
                ("opp", (t.get("region") or "").lower() + ":" + topic_id,
                 payload.get("updated") or "")
            )
            return {"opportunity": opportunity if isinstance(opportunity, str) else ""}
    raise HTTPException(status_code=404, detail="No such topic on the desk")


@app.get("/api/prewarm")
def api_prewarm(request: Request, _gate: None = Depends(require_passcode)) -> dict:
    _check_rate_limit(request)
    """Warm the desk cache for all four regions in one call.

    One Cloud Scheduler job hits this after the morning engine cron, so the
    first human request of the day serves from a hot cache. Targets build in
    parallel; a failure in one target never blocks the others. Beyond the desk,
    this also warms the other endpoints the morning QA and the first human hit
    pay cold-cache tax on (today, rails, voices, metrics, intel overview)."""

    def _count(payload: dict, key: str) -> int:
        value = payload.get(key)
        return len(value) if isinstance(value, (list, dict)) else 0

    targets: dict = {}
    for region in VALID_MARKETS:
        targets["desk:" + region] = lambda r=region: _count(
            _desk_cached(r, lambda r=r: desk.build_desk_payload(r)), "topics"
        )
        targets["today:" + region] = lambda r=region: _count(
            _market_cached("today", r, lambda: bq.fetch_today(r)), "briefs"
        )
        targets["rails:" + region] = lambda r=region: _count(
            _market_cached("rails", r, lambda: bq.fetch_rails(r)), "rails"
        )
        targets["voices:" + region] = lambda r=region: _count(
            _market_cached("voices", r, lambda: creator.build_voices(r)), "voices"
        )
    targets["metrics:all"] = lambda: int(
        _metrics_cached(lambda: bq.fetch_growth_metrics()).get("platforms") or 0
    )

    warmed: dict = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {name: pool.submit(fn) for name, fn in targets.items()}
    for name, future in futures.items():
        try:
            warmed[name] = future.result()
        except Exception as exc:
            logger.warning("prewarm failed for %s: %s", name, exc)
            warmed[name] = None

    topic_counts = {region: 0 for region in VALID_MARKETS}
    topic_failures: set[str] = set()
    topic_targets: dict = {}
    for region in VALID_MARKETS:
        try:
            payload = _desk_cached(region, lambda r=region: desk.build_desk_payload(r))
        except Exception as exc:
            logger.warning("prewarm topic list failed for %s: %s", region, exc)
            topic_failures.add(region)
            continue
        topic_ids = {
            str(row.get("id") or "").strip()
            for row in payload.get("topics", [])
            if isinstance(row, dict) and row.get("id")
        }
        for topic_id in topic_ids:
            topic_targets[(region, topic_id)] = lambda t=topic_id, r=region: (
                _topic_profile(t, r)
            )

    with ThreadPoolExecutor(max_workers=6) as pool:
        topic_futures = {key: pool.submit(fn) for key, fn in topic_targets.items()}
    for (region, topic_id), future in topic_futures.items():
        try:
            if future.result() is not None:
                topic_counts[region] += 1
        except Exception as exc:
            logger.warning("prewarm topic failed for %s/%s: %s", region, topic_id, exc)
            topic_failures.add(region)
    for region in VALID_MARKETS:
        warmed["topic_profiles:" + region] = (
            None if region in topic_failures else topic_counts[region]
        )
    return {"ok": all(v is not None for v in warmed.values()), "warmed": warmed}


class BriefRequest(BaseModel):
    query: str = ""
    region: str = "all"


GENERATION_MAX_CONCURRENT_DEFAULT = 4
GENERATION_MAX_CONCURRENT_LIMIT = 32
_generation_active = 0
_generation_capacity_lock = threading.Lock()


def _generation_capacity_limit() -> int:
    raw = os.environ.get("GENERATION_MAX_CONCURRENT", "").strip()
    try:
        value = int(raw) if raw else GENERATION_MAX_CONCURRENT_DEFAULT
    except ValueError:
        value = GENERATION_MAX_CONCURRENT_DEFAULT
    return max(1, min(value, GENERATION_MAX_CONCURRENT_LIMIT))


def _try_acquire_generation_capacity() -> bool:
    global _generation_active
    with _generation_capacity_lock:
        if _generation_active >= _generation_capacity_limit():
            return False
        _generation_active += 1
        return True


def _release_generation_capacity() -> None:
    global _generation_active
    with _generation_capacity_lock:
        _generation_active = max(0, _generation_active - 1)


def _capacity_busy_response() -> JSONResponse:
    return JSONResponse(
        {
            "error": "capacity_busy",
            "message": "The desk is at capacity. Try again shortly.",
            "retry_after_seconds": 30,
        },
        status_code=429,
        headers={"Retry-After": "30"},
    )


def _start_generation_thread(jobs: set, key, target, args: tuple) -> None:
    try:
        threading.Thread(target=target, args=args, daemon=True).start()
    except Exception:
        jobs.discard(key)
        _release_generation_capacity()
        raise


# Brief generation runs in a background thread so the POST returns inside the
# corporate-proxy timeout window. The running-set guards duplicate threads per
# (query, region) key; a recorded failure carries a short TTL so retry works.
BRIEF_ERROR_TTL_SECONDS = 30.0
_brief_jobs: set = set()
_brief_jobs_lock = threading.Lock()


def _brief_is_error(value) -> bool:
    return isinstance(value, dict) and value.get("error") is True


def _run_brief_job(key, query: str, region: str) -> None:
    """Generate one brief and land it in the answer cache.

    A None payload or a raised exception is recorded as {"error": true} with a
    short TTL: the status poller surfaces it once, and a retry a moment later
    starts a fresh generation instead of replaying the pinned failure."""
    try:
        payload = desk.build_ask_brief(query, region)
        if payload is None:
            synth.cache_set(key, {"error": True}, ttl=BRIEF_ERROR_TTL_SECONDS)
        else:
            synth.cache_set(key, payload)
    except Exception:
        synth.cache_set(key, {"error": True}, ttl=BRIEF_ERROR_TTL_SECONDS)
    finally:
        with _brief_jobs_lock:
            _brief_jobs.discard(key)
        _release_generation_capacity()


@app.post("/api/ask/brief")
def api_ask_brief(
    request: Request, body: BriefRequest, _gate: None = Depends(require_passcode)
):
    _check_rate_limit(request)
    _validate_market(body.region)
    query = _bounded_search_text(bq.normalize_query(body.query), "query")
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    # The "brief" prefix keeps live-brief payloads from colliding with the
    # legacy /api/ask answers, which share the same cache and key space.
    key = ("brief", query, body.region)
    cached = synth.cache_get(key)
    if cached is not None and not _brief_is_error(cached):
        return cached
    # Cache miss (or a recorded failure, which a fresh ask retries): start the
    # generation in the background and answer immediately so the request never
    # outlives a proxy timeout. The poller on /api/ask/brief/status collects.
    with _brief_jobs_lock:
        if key not in _brief_jobs:
            if not _try_acquire_generation_capacity():
                return _capacity_busy_response()
            _brief_jobs.add(key)
            _start_generation_thread(
                _brief_jobs, key, _run_brief_job, (key, query, body.region)
            )
    return JSONResponse({"pending": True}, status_code=202)


@app.get("/api/ask/brief/status")
def api_ask_brief_status(
    query: str = Query(default=""),
    region: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    """Fast poll target for an in-flight brief: pending while the job runs,
    the full brief when it lands, {"error": true} on a recorded failure."""
    _validate_market(region)
    normalized = bq.normalize_query(query)
    if not normalized:
        raise HTTPException(status_code=400, detail="query is required")
    key = ("brief", normalized, region)
    cached = synth.cache_get(key)
    if cached is not None:
        if _brief_is_error(cached):
            return {"error": True}
        return cached
    return {"pending": True}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = ""
    history: list = []
    market: str = "za"
    parent_request_id: StrictStr | None = None
    thread_anchor_request_id: StrictStr | None = None
    # Registered with the question submission contract: the routes own its
    # grammar and the derivation of the request identity, it never enters the
    # admission envelope, and an identical resubmission joins the same request.
    idempotency_key: StrictStr | None = None
    # The optional client lens the Console names. General 42 is the default, so
    # an absent value is not a lens; the routes resolve any named one against
    # the authorized registry for the server scope before admission.
    client_lens_id: StrictStr | None = None


# Intelligence Centre chat. The function-calling loop runs in a background
# thread so the POST returns inside the corporate-proxy timeout window, the same
# reason the brief jobs run detached. The result lands in the shared cache,
# which persists to GCS, so a status poll the load balancer routes to a
# different Cloud Run instance than ran the job still finds the answer. The
# service runs more than one instance, so an in-process-only result hung every
# poll that missed the origin instance. The job id is a fresh uuid per send, so
# unlike the deterministic brief keys it never collides across deploys or
# instances, which is what makes persisting it safe.
_chat_jobs: set = set()
_chat_jobs_lock = threading.Lock()


def _chat_market(market: str) -> str:
    # "all" is a valid console desk: run_chat fans the model's tool calls across
    # za/ng/ke and reports each market distinctly (markets never blend).
    if market != "all" and market not in SINGLE_MARKETS:
        raise HTTPException(status_code=400, detail="market must be za, ng, ke, or all")
    return market


def _run_chat_job(key, message: str, history: list, market: str) -> None:
    """Run the chat loop and land the result in the shared answer cache.

    Both the answer and a failure persist to the GCS-backed cache so any
    instance's status poll resolves, not only the one that ran the job. A raised
    exception lands as {"error": true}; the unique job id means a retry uses a
    fresh id and never reads the failure back, so it needs no short ttl."""
    try:
        synth.cache_set(key, chat.run_chat(message, history, market))
    except Exception as error:
        logger.error("chat job %s failed (market=%s, error_type=%s)", key, market, type(error).__name__)
        synth.cache_set(key, {"error": True})
    finally:
        with _chat_jobs_lock:
            _chat_jobs.discard(key)
        _release_generation_capacity()


@app.post("/api/chat/send")
async def api_chat_send(
    request: Request, body: ChatRequest, _gate: None = Depends(require_passcode)
):
    """Admit one durable question and return its existing public job envelope."""
    _check_rate_limit(request)
    result = await general_question_routes.start_route(request, body.model_dump(exclude_none=True))
    return JSONResponse(result, status_code=202)


@app.get("/api/chat/lenses")
async def api_chat_lenses(_gate: None = Depends(require_passcode)) -> dict:
    """The client lenses this server scope authorizes; general 42 is the default."""
    return general_question_routes.client_lens_roster()


@app.get("/api/chat/coverage")
async def api_chat_coverage(request: Request, _gate: None = Depends(require_passcode)) -> dict:
    """The observation window a fresh question can be answered for under this worker."""
    return await general_question_routes.coverage_route(request)


@app.get("/api/chat/status")
async def api_chat_status(
    request: Request,
    response: Response,
    job_id: str = Query(default=""),
    _gate: None = Depends(require_passcode),
) -> dict:
    """Project only scope-bound durable engine status and verified result bytes.

    A terminal body also names its rendered response digest in a header, which
    an advisory delivery report quotes back; the body itself is unchanged.
    """
    served = await general_question_routes.status_route(request, job_id)
    digest = general_question_routes.served_response_digest(served)
    if digest is not None:
        response.headers["X-Question-Response-Digest"] = digest
    return served


@app.post("/api/internal/v2/question-delivery", include_in_schema=False)
async def api_question_delivery(
    request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """One advisory browser report that a stored answer was rendered."""
    return await general_question_routes.delivery_route(
        request, client_key=_client_ip(request)
    )


@app.get("/api/internal/v2/question-delivery/{request_id}", include_in_schema=False)
async def api_question_delivery_status(
    request_id: str, request: Request, _gate: None = Depends(require_passcode)
) -> Response:
    """Delivery as this process heard it, for a request known in this scope only."""
    return await general_question_routes.delivery_status_route(request, request_id)


def _answer_export_refusal(refused: ask_export.AskExportRefused) -> JSONResponse:
    return JSONResponse(
        {"detail": {"code": refused.code, "message": refused.message}},
        status_code=refused.status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _answer_export_page(request: Request, request_id: str, *, pdf: bool) -> str:
    """The export page rebuilt from the stored reply the worker reads for this scope."""
    from src.api.question_worker_process import WorkerProcessError
    from src.api.question_worker_protocol import _uuid

    if not _uuid(request_id):
        raise ask_export.AskExportRefused("request_invalid")
    try:
        # The worker only reads back a request stored under the scope it is
        # given, so this is the client scope the stored request carries.
        client_scope_id = general_question_routes._current_scope()["client_scope_id"]
        detail = await general_question_routes.read_question_observation(
            request, request_id
        )
    except WorkerProcessError as error:
        if error.reason == "observation_request_unavailable":
            raise ask_export.AskExportRefused("request_unavailable") from None
        raise ask_export.AskExportRefused("export_unavailable") from None
    except Exception:
        raise ask_export.AskExportRefused("export_unavailable") from None
    return ask_export.render_answer_html(
        detail, request_id, pdf=pdf, client_scope_id=client_scope_id
    )


@app.get("/api/chat/answer/{request_id}/export.html", include_in_schema=False)
async def api_chat_answer_export_html(
    request: Request, request_id: str, _gate: None = Depends(require_passcode)
) -> Response:
    """A downloadable copy of one completed Ask answer with its cited sources."""
    response = await _answer_export_html(request, request_id)
    _emit_artifact_outcome(
        response,
        stage="artifact_export",
        action="export_html",
        request_id=request_id,
        format="html",
    )
    return response


async def _answer_export_html(request: Request, request_id: str) -> Response:
    try:
        page = await _answer_export_page(request, request_id, pdf=False)
    except ask_export.AskExportRefused as refused:
        return _answer_export_refusal(refused)
    return HTMLResponse(
        page,
        headers={
            "Content-Disposition": f'attachment; filename="42-answer-{request_id[:8]}.html"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/chat/answer/{request_id}/export.pdf", include_in_schema=False)
async def api_chat_answer_export_pdf(
    request: Request, request_id: str, _gate: None = Depends(require_passcode)
) -> Response:
    """The same copy as a PDF, made by the reviewed renderer and nothing else."""
    response = await _answer_export_pdf(request, request_id)
    _emit_artifact_outcome(
        response,
        stage="artifact_export",
        action="export_pdf",
        request_id=request_id,
        format="pdf",
    )
    return response


async def _answer_export_pdf(request: Request, request_id: str) -> Response:
    try:
        page = await _answer_export_page(request, request_id, pdf=True)
    except ask_export.AskExportRefused as refused:
        return _answer_export_refusal(refused)
    try:
        data = await asyncio.to_thread(
            pdf_exporter.render_stored_html_pdf,
            page.encode("utf-8"),
            asset_root=ask_export.PDF_ASSET_ROOT,
        )
    except pdf_exporter.PdfExportError as error:
        logger.warning("answer pdf export refused reason=%s", error.reason)
        code = "pdf_busy" if error.reason == "pdf_render_busy" else "pdf_unavailable"
        return _answer_export_refusal(ask_export.AskExportRefused(code))
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="42-answer-{request_id[:8]}.pdf"',
            "Cache-Control": "no-store",
        },
    )


app.add_api_route(
    "/internal/general-question/execute", general_question_routes.execute_route,
    methods=["POST"], include_in_schema=False,
)


class RefineRequest(BaseModel):
    topic: dict = {}
    instruction: str = ""


@app.post("/api/ask/refine")
def api_ask_refine(
    request: Request, body: RefineRequest, _gate: None = Depends(require_passcode)
) -> dict:
    """Rework a generated brief's prose to an instruction. Never cached: each
    refinement is a fresh instruction against the caller's current brief."""
    _check_rate_limit(request)
    instruction = (body.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction is required")
    if len(instruction) > MAX_REFINE_INSTRUCTION_CHARS:
        raise HTTPException(status_code=400, detail="instruction is too long")
    topic = body.topic if isinstance(body.topic, dict) else {}
    if not topic.get("generated") or not isinstance(topic.get("brief"), dict):
        raise HTTPException(status_code=400, detail="topic must be a generated brief")
    payload = desk.refine_ask_brief(topic, instruction)
    if payload is None:
        raise HTTPException(
            status_code=502, detail="Brief refinement is unavailable right now"
        )
    return payload


@app.get("/api/ask")
def api_ask(
    request: Request,
    q: str = Query(default=""),
    market: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    _check_rate_limit(request)
    _validate_market(market)
    query = _bounded_search_text(bq.normalize_query(q), "q")
    if not query:
        raise HTTPException(status_code=400, detail="q is required")
    return _ask_payload_cached(query, market)


@app.get("/card/{query}", response_class=HTMLResponse)
def card_page(
    request: Request,
    query: str,
    market: str = "all",
    t: str | None = Query(default=None),
) -> HTMLResponse:
    # The card reads archive evidence with the same rate limit as /api/ask.
    # A printable link cannot send a header, so it
    # authenticates with a per-card HMAC token (t) instead of the raw passcode:
    # the token is forwardable but useless for any other card or for /api/*, so
    # the master passcode never rides in a URL (and never lands in access logs,
    # history, or a Referer). The X-Passcode header still works for the in-app
    # open. Mint a shareable link via GET /api/card-link.
    _check_rate_limit(request)
    _validate_market(market)
    normalized = _bounded_search_text(bq.normalize_query(query), "The query")
    if not normalized:
        raise HTTPException(status_code=400, detail="A query is required")
    expected = os.environ.get("UI_PASSCODE", "")
    if expected:
        header_pass = request.headers.get("X-Passcode", "")
        token_ok = bool(t) and hmac.compare_digest(t, _card_token(normalized, market))
        header_ok = hmac.compare_digest(
            header_pass.encode("utf-8"), expected.encode("utf-8")
        )
        if not (token_ok or header_ok):
            return HTMLResponse(CARD_LOCKED_HTML, status_code=401)
    elif not _gate_open():
        return HTMLResponse(CARD_LOCKED_HTML, status_code=503)
    payload = _ask_payload_cached(normalized, market)
    html_doc = card_render.render_card(payload, datetime.now(UTC))
    return HTMLResponse(html_doc)


@app.get("/api/card-link")
def card_link(
    request: Request,
    q: str,
    market: str = "all",
    _gate: None = Depends(require_passcode),
) -> dict:
    # Header-gated: an authenticated user mints a shareable, forwardable card
    # URL whose per-card token carries no master secret.
    _validate_market(market)
    normalized = _bounded_search_text(bq.normalize_query(q), "q")
    if not normalized:
        raise HTTPException(status_code=400, detail="q is required")
    return {
        "url": f"/card/{quote(normalized, safe='')}?market={market}&t={_card_token(normalized, market)}"
    }


class ResearchGenerateRequest(BaseModel):
    persona_id: str = ""
    markets: list = []
    product_frame: str | None = None
    focus_query_groups: list = []
    focus_note: str = ""
    focus_examples: list = []
    parent_artifact_id: str = ""


class ResearchGenerateBatchRequest(BaseModel):
    persona_id: str = ""
    product_frame: str | None = None
    behaviours: list = []


class ResearchGenerateConsolidatedRequest(BaseModel):
    persona_id: str = ""
    product_frame: str | None = None
    markets: list = []
    behaviours: list = []
    seo_notes: str = ""


class ResearchRefineRequest(BaseModel):
    artifact_id: str = ""
    instruction: str = ""


class ResearchExportHtmlRequest(BaseModel):
    doc_json: dict
    persona_label: str = ""
    markets: list = []
    artifact_id: str = ""
    sources: list = []


def _neutral_research_doc(doc_json: dict | None) -> dict:
    """Copy a persisted research doc into the audience-neutral response contract."""
    payload = dict(doc_json) if isinstance(doc_json, dict) else {}
    grounded_raw = payload.get("grounded")
    grounded = dict(grounded_raw) if isinstance(grounded_raw, dict) else {}
    if "voice_of_genz" in grounded:
        legacy_voice = grounded.pop("voice_of_genz")
        if not grounded.get("voice_read"):
            grounded["voice_read"] = legacy_voice
    payload["grounded"] = grounded
    return payload


def _research_html_attachment(
    doc_json: dict,
    *,
    persona_label: str = "",
    markets: list | None = None,
    slug: str = "brief",
    sources: list | None = None,
) -> HTMLResponse:
    payload = _neutral_research_doc(doc_json)
    refs = payload.get("_refs") or list(sources or [])
    if refs and not payload.get("_refs"):
        payload = {**payload, "_refs": refs}
    html_doc = synth.render_research_html(
        payload,
        persona_label=str(persona_label or ""),
        markets=markets or [],
    )
    # The slug can come from the request body, so only filename safe characters
    # reach the header: no quote, separator, dot pair or non Latin-1 character.
    safe_slug = re.sub(r"[^A-Za-z0-9_-]+", "", slug or "")[:12] or "brief"
    headers = {
        "Content-Disposition": f'attachment; filename="42-brief-{safe_slug}.html"',
        "Cache-Control": "no-store",
    }
    return HTMLResponse(html_doc, headers=headers)


_research_jobs: set = set()
_research_jobs_lock = threading.Lock()


def _run_research_job(
    key,
    persona_id: str,
    markets: list,
    product_frame: str | None,
    focus_query_groups: list | None = None,
    focus_note: str = "",
    parent_artifact_id: str | None = None,
    focus_examples: list | None = None,
) -> None:
    try:
        synth.cache_set(
            key, {"pending": True, "phase": "gathering", "status": "gathering"}, ttl=120
        )
        result = research.run_research_job(
            persona_id,
            markets or None,
            product_frame,
            progress_key=key,
            focus_query_groups=focus_query_groups or None,
            focus_note=focus_note or "",
            focus_examples=focus_examples or None,
        )
        if result.get("error"):
            synth.cache_set(
                key,
                {
                    "error": True,
                    "status": "failed",
                    "message": result.get("message") or "Research generation failed.",
                    **({"code": result["code"]} if result.get("code") else {}),
                },
                ttl=120,
            )
            return
        research.persist_job_result(
            result, parent_artifact_id=parent_artifact_id or None
        )
        synth.cache_set(
            key,
            {
                "status": result.get("status"),
                "degraded_reason": result.get("degraded_reason"),
                "doc": result.get("doc"),
                "signal_quality": result.get("signal_quality"),
                "artifact_id": result.get("artifact_id"),
                "phase": "completed",
            },
        )
    except Exception as exc:
        logger.exception("research job failed persona=%s", persona_id)
        synth.cache_set(
            key,
            {
                "error": True,
                "status": "failed",
                **research.research_failure(exc),
            },
            ttl=120,
        )
    finally:
        with _research_jobs_lock:
            _research_jobs.discard(key)
        _release_generation_capacity()


def _run_research_batch_job(
    key,
    persona_id: str,
    product_frame: str | None,
    rows: list,
) -> None:
    """One rate-limit slot; N sequential briefs for approved behaviour rows."""
    total = len(rows)
    parent_id = None
    batch_results: list = []
    batch_ttl = 600
    try:
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            label = research.behaviour_brief_label(row, i)
            research._batch_progress_meta[key] = {
                "batch_index": i,
                "batch_total": total,
                "batch_label": label,
            }
            synth.cache_set(
                key,
                {
                    "pending": True,
                    "phase": "running",
                    "status": "gathering_seeds",
                    "batch_index": i,
                    "batch_total": total,
                    "batch_label": label,
                },
                ttl=batch_ttl,
            )
            groups, note = research.focus_payload_for_behaviour(row)
            mk = str(row.get("market") or "").strip().lower()
            mks = [mk] if mk in VALID_MARKETS else []
            examples = (
                row.get("examples") if isinstance(row.get("examples"), list) else []
            )
            result = research.run_research_job(
                persona_id,
                mks or None,
                product_frame,
                progress_key=key,
                focus_query_groups=groups or None,
                focus_note=note,
                focus_examples=examples or None,
            )
            if result.get("error"):
                batch_results.append(
                    {
                        "behaviour": row,
                        "status": "failed",
                        "message": result.get("message")
                        or "Research generation failed.",
                        **({"code": result["code"]} if result.get("code") else {}),
                    }
                )
                continue
            aid = research.persist_job_result(
                result, parent_artifact_id=parent_id or None
            )
            if not parent_id:
                parent_id = aid
            batch_results.append(
                {
                    "behaviour": row,
                    "status": result.get("status"),
                    "artifact_id": aid,
                    "doc": result.get("doc"),
                    "signal_quality": result.get("signal_quality"),
                    "evidence_graph": result.get("evidence_graph"),
                    "degraded_reason": result.get("degraded_reason"),
                }
            )
        if not batch_results:
            synth.cache_set(
                key,
                {
                    "error": True,
                    "status": "failed",
                    "message": "No briefs were generated.",
                },
                ttl=batch_ttl,
            )
            return
        ok = [r for r in batch_results if r.get("artifact_id")]
        if not ok:
            synth.cache_set(
                key,
                {
                    "error": True,
                    "status": "failed",
                    "message": batch_results[0].get("message")
                    or "Batch generation failed.",
                    **(
                        {"code": batch_results[0]["code"]}
                        if batch_results[0].get("code")
                        else {}
                    ),
                    "batch_results": batch_results,
                },
                ttl=batch_ttl,
            )
            return
        synth.cache_set(
            key,
            {
                "status": "completed",
                "phase": "completed",
                "batch_results": batch_results,
                "parent_artifact_id": parent_id,
                "artifact_id": parent_id,
                "doc": ok[0].get("doc"),
                "signal_quality": ok[0].get("signal_quality"),
                "evidence_graph": ok[0].get("evidence_graph"),
                "degraded_reason": ok[0].get("degraded_reason"),
            },
            ttl=batch_ttl,
        )
    except Exception as exc:
        logger.exception("research batch job failed persona=%s", persona_id)
        synth.cache_set(
            key,
            {
                "error": True,
                "status": "failed",
                **research.research_failure(exc, "Batch research generation failed."),
                "batch_results": batch_results,
            },
            ttl=batch_ttl,
        )
    finally:
        research._batch_progress_meta.pop(key, None)
        with _research_jobs_lock:
            _research_jobs.discard(key)
        _release_generation_capacity()


def _run_research_consolidated_job(
    key,
    persona_id: str,
    product_frame: str | None,
    markets: list,
    rows: list,
    seo_notes: str = "",
) -> None:
    try:
        synth.cache_set(
            key,
            {"pending": True, "phase": "gathering", "status": "gathering_seeds"},
            ttl=600,
        )
        result = research.run_consolidated_research_job(
            persona_id,
            rows,
            markets or None,
            product_frame,
            progress_key=key,
            seo_notes=seo_notes,
        )
        if result.get("error"):
            synth.cache_set(
                key,
                {
                    "error": True,
                    "status": "failed",
                    "message": result.get("message")
                    or "Consolidated research generation failed.",
                    **({"code": result["code"]} if result.get("code") else {}),
                },
                ttl=120,
            )
            return
        research.persist_consolidated_result(result)
        synth.cache_set(
            key,
            {
                "status": result.get("status"),
                "degraded_reason": result.get("degraded_reason"),
                "doc": result.get("doc"),
                "signal_quality": result.get("signal_quality"),
                "artifact_id": result.get("artifact_id"),
                "evidence_graph": result.get("evidence_graph"),
                "phase": "completed",
                "consolidated": True,
            },
            ttl=600,
        )
    except Exception as exc:
        logger.exception("consolidated research job failed persona=%s", persona_id)
        synth.cache_set(
            key,
            {
                "error": True,
                "status": "failed",
                **research.research_failure(
                    exc, "Consolidated research generation failed."
                ),
            },
            ttl=120,
        )
    finally:
        with _research_jobs_lock:
            _research_jobs.discard(key)
        _release_generation_capacity()


def _brief_writing_refusal() -> JSONResponse | None:
    """Staging refuses the brief writer before a job, a slot or a rate hit is spent."""
    if research.brief_writing_available():
        return None
    return JSONResponse(
        {
            "detail": {
                "code": research.BRIEF_WRITING_UNAVAILABLE,
                "message": research.BRIEF_WRITING_UNAVAILABLE_MESSAGE,
            }
        },
        status_code=503,
    )


@app.get("/api/research/availability")
def api_research_availability(_gate: None = Depends(require_passcode)) -> dict:
    """What the Build brief flow can do on this deployment, in reader words."""
    writing = research.brief_writing_available()
    scan = _behaviour_scan_enabled()
    return {
        "brief_writing": {
            "available": writing,
            "code": None if writing else research.BRIEF_WRITING_UNAVAILABLE,
            "message": None if writing else research.BRIEF_WRITING_UNAVAILABLE_MESSAGE,
        },
        "behaviour_scan": {
            "available": scan,
            "code": None if scan else research.BEHAVIOUR_SCAN_OFF,
            "message": None if scan else research.BEHAVIOUR_SCAN_OFF_MESSAGE,
        },
        # Where only the reviewed question engine may call the model, the page
        # asks it for the brief through Ask's own start path.
        "brief_via_question": research.brief_via_question(),
    }


@app.get("/api/research/personas")
def api_research_personas(_gate: None = Depends(require_passcode)) -> dict:
    return {"personas": persona_registry.list_enabled_personas()}


@app.post("/api/research/generate")
def api_research_generate(
    request: Request,
    body: ResearchGenerateRequest,
    _gate: None = Depends(require_passcode),
):
    refusal = _brief_writing_refusal()
    if refusal is not None:
        return refusal
    _check_research_rate_limit(request, "generate")
    persona_id = (body.persona_id or "").strip()
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id is required")
    if persona_registry.get_persona(persona_id) is None:
        raise HTTPException(
            status_code=400, detail=f"unknown or disabled persona: {persona_id}"
        )
    markets = [str(m).lower() for m in (body.markets or []) if m]
    for mk in markets:
        if mk not in VALID_MARKETS or mk == "all":
            raise HTTPException(
                status_code=400, detail=f"invalid research market: {mk}"
            )
    product_frame = (
        body.product_frame.strip() if body.product_frame is not None else None
    )
    focus_query_groups = [
        str(g).strip() for g in (body.focus_query_groups or []) if str(g).strip()
    ][:12]
    focus_note = (body.focus_note or "").strip()[:600]
    parent_artifact_id = (body.parent_artifact_id or "").strip()[:128] or None
    focus_examples = (
        body.focus_examples if isinstance(body.focus_examples, list) else []
    )
    focus_examples = focus_examples[:6]
    if not _try_acquire_generation_capacity():
        return _capacity_busy_response()
    with _research_jobs_lock:
        job_id = "research_" + uuid.uuid4().hex
        key = ("research", job_id)
        _research_jobs.add(key)
        _start_generation_thread(
            _research_jobs,
            key,
            _run_research_job,
            (
                key,
                persona_id,
                markets,
                product_frame,
                focus_query_groups,
                focus_note,
                parent_artifact_id,
                focus_examples,
            ),
        )
    return JSONResponse({"job_id": job_id, "phase": "gathering"}, status_code=202)


@app.post("/api/research/generate-batch")
def api_research_generate_batch(
    request: Request,
    body: ResearchGenerateBatchRequest,
    _gate: None = Depends(require_passcode),
):
    refusal = _brief_writing_refusal()
    if refusal is not None:
        return refusal
    _check_research_rate_limit(request, "batch_generate")
    persona_id = (body.persona_id or "").strip()
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id is required")
    if persona_registry.get_persona(persona_id) is None:
        raise HTTPException(
            status_code=400, detail=f"unknown or disabled persona: {persona_id}"
        )
    rows = [r for r in (body.behaviours or []) if isinstance(r, dict)][:12]
    if not rows:
        raise HTTPException(status_code=400, detail="behaviours is required")
    product_frame = (
        body.product_frame.strip() if body.product_frame is not None else None
    )
    if not _try_acquire_generation_capacity():
        return _capacity_busy_response()
    with _research_jobs_lock:
        job_id = "research_batch_" + uuid.uuid4().hex
        key = ("research", job_id)
        _research_jobs.add(key)
        _start_generation_thread(
            _research_jobs,
            key,
            _run_research_batch_job,
            (key, persona_id, product_frame, rows),
        )
    return JSONResponse(
        {"job_id": job_id, "phase": "gathering", "batch_total": len(rows)},
        status_code=202,
    )


@app.post("/api/research/generate-consolidated")
def api_research_generate_consolidated(
    request: Request,
    body: ResearchGenerateConsolidatedRequest,
    _gate: None = Depends(require_passcode),
):
    refusal = _brief_writing_refusal()
    if refusal is not None:
        return refusal
    _check_research_rate_limit(request, "batch_generate")
    persona_id = (body.persona_id or "").strip()
    if not persona_id:
        raise HTTPException(status_code=400, detail="persona_id is required")
    if persona_registry.get_persona(persona_id) is None:
        raise HTTPException(
            status_code=400, detail=f"unknown or disabled persona: {persona_id}"
        )
    rows = [r for r in (body.behaviours or []) if isinstance(r, dict)][:12]
    if not rows:
        raise HTTPException(status_code=400, detail="behaviours is required")
    markets = [str(m).lower() for m in (body.markets or []) if m]
    for mk in markets:
        if mk not in VALID_MARKETS or mk == "all":
            raise HTTPException(
                status_code=400, detail=f"invalid research market: {mk}"
            )
    product_frame = (
        body.product_frame.strip() if body.product_frame is not None else None
    )
    seo_notes = (body.seo_notes or "").strip()[:1200]
    if not _try_acquire_generation_capacity():
        return _capacity_busy_response()
    with _research_jobs_lock:
        job_id = "research_consolidated_" + uuid.uuid4().hex
        key = ("research", job_id)
        _research_jobs.add(key)
        _start_generation_thread(
            _research_jobs,
            key,
            _run_research_consolidated_job,
            (key, persona_id, product_frame, markets, rows, seo_notes),
        )
    return JSONResponse(
        {
            "job_id": job_id,
            "phase": "gathering",
            "behaviour_total": len(rows),
            "consolidated": True,
        },
        status_code=202,
    )


@app.get("/api/research/status")
def api_research_status(
    job_id: str = Query(default=""), _gate: None = Depends(require_passcode)
) -> dict:
    job_id = (job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    key = ("research", job_id)
    cached = synth.cache_get(key)
    if cached is None:
        return {"pending": True, "phase": "gathering", "status": "gathering"}
    if isinstance(cached, dict) and cached.get("pending"):
        return cached
    if isinstance(cached, dict) and cached.get("error") is True:
        return {
            "error": True,
            "status": "failed",
            "message": cached.get("message") or "Research generation failed.",
            **({"code": cached["code"]} if cached.get("code") else {}),
        }
    return cached


def _behaviour_scan_enabled() -> bool:
    return os.environ.get("BEHAVIOUR_SCAN_ENABLED", "").strip().lower() == "true"


@app.get("/api/research/behaviours")
def api_research_behaviours(
    request: Request,
    markets: str = Query(default=""),
    per_market: int = Query(default=behaviours.DEFAULT_PER_MARKET),
    _gate: None = Depends(require_passcode),
) -> dict:
    if not _behaviour_scan_enabled():
        raise HTTPException(
            status_code=404,
            detail={
                "code": research.BEHAVIOUR_SCAN_OFF,
                "message": research.BEHAVIOUR_SCAN_OFF_MESSAGE,
            },
        )
    _check_research_rate_limit(request, "behaviours")
    mks = [m.strip().lower() for m in (markets or "").split(",") if m.strip()]
    if not mks:
        raise HTTPException(status_code=400, detail="markets is required")
    for mk in mks:
        if mk not in VALID_MARKETS or mk == "all":
            raise HTTPException(status_code=400, detail=f"invalid market: {mk}")
    per = max(1, min(int(per_market or behaviours.DEFAULT_PER_MARKET), 25))
    return behaviours.scan_market_behaviours(mks, per)


@app.get("/api/research/recent")
def api_research_recent(
    limit: int = Query(default=20), _gate: None = Depends(require_passcode)
) -> dict:
    lim = max(1, min(int(limit or 20), 50))
    return {
        "artifacts": [
            {key: value for key, value in item.items() if key != "client_scope_id"}
            for item in bq.list_recent_research_artifacts(lim)
            if _research_row_in_scope(item)
        ]
    }


@app.post("/api/research/export-html", response_class=HTMLResponse)
def api_research_export_html_post(
    body: ResearchExportHtmlRequest, _gate: None = Depends(require_passcode)
) -> HTMLResponse:
    doc_json = body.doc_json if isinstance(body.doc_json, dict) else {}
    if not doc_json:
        raise HTTPException(status_code=400, detail="doc_json is required")
    slug = (body.artifact_id or "brief").strip()[:12] or "brief"
    return _research_html_attachment(
        doc_json,
        persona_label=body.persona_label,
        markets=body.markets,
        slug=slug,
        sources=body.sources,
    )


@app.get("/api/research/{artifact_id}/export.html", response_class=HTMLResponse)
def api_research_export_html(
    artifact_id: str, _gate: None = Depends(require_passcode)
) -> HTMLResponse:
    try:
        row = _research_artifact_row(artifact_id)
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    doc_json = _neutral_research_doc(row.get("doc_json") or row.get("synthesis"))
    evidence_graph = row.get("evidence_graph") or {"refs": row.get("evidence")}
    refs = evidence_graph.get("refs") or row.get("evidence") or []
    return _research_html_attachment(
        doc_json,
        persona_label=str(row.get("persona_label") or ""),
        markets=row.get("markets") or [],
        slug=(artifact_id or "brief")[:12],
        sources=refs,
    )


@app.get("/api/research/{artifact_id}/evidence-pack.html", response_class=HTMLResponse)
def api_research_evidence_pack(
    artifact_id: str, _gate: None = Depends(require_passcode)
) -> HTMLResponse:
    try:
        row = _research_artifact_row(artifact_id)
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    doc_json = _neutral_research_doc(row.get("doc_json") or row.get("synthesis"))
    evidence_graph = row.get("evidence_graph") or {"refs": row.get("evidence") or []}
    html_doc = synth.render_evidence_pack_html(doc_json, evidence_graph)
    slug = (artifact_id or "pack")[:12]
    headers = {
        "Content-Disposition": f'inline; filename="42-evidence-{slug}.html"',
        "Cache-Control": "no-store",
    }
    return HTMLResponse(html_doc, headers=headers)


@app.get("/api/research/{artifact_id}")
def api_research_artifact(
    artifact_id: str, _gate: None = Depends(require_passcode)
) -> Response:
    try:
        row = _research_artifact_row(artifact_id)
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    evidence_graph = row.get("evidence_graph") or {"refs": row.get("evidence")}
    refs = evidence_graph.get("refs") or row.get("evidence") or []
    return JSONResponse(
        {
            "artifact_id": row.get("artifact_id"),
            "doc": {
                "json": _neutral_research_doc(row.get("doc_json") or row.get("synthesis")),
                "markdown": row.get("doc_markdown") or row.get("markdown"),
                "sources": refs,
            },
            "evidence_graph": evidence_graph,
            "signal_quality": row.get("signal_quality")
            or (row.get("quality") or {}).get("level"),
            "degraded_reason": row.get("degraded_reason") or "",
            "persona_id": row.get("persona_id"),
            "persona_label": row.get("persona_label"),
            "markets": row.get("markets"),
        }
    )


@app.post("/api/research/refine")
def api_research_refine(
    request: Request,
    body: ResearchRefineRequest,
    _gate: None = Depends(require_passcode),
):
    _check_research_rate_limit(request, "refine")
    aid = (body.artifact_id or "").strip()
    instruction = (body.instruction or "").strip()
    if not aid or not instruction:
        raise HTTPException(
            status_code=400, detail="artifact_id and instruction are required"
        )
    try:
        row = _research_artifact_row(aid)
    except _ReviewRefused as refused:
        return _review_refusal(refused)
    doc_json = _neutral_research_doc(row.get("doc_json") or row.get("synthesis"))
    artifact = {**row, "doc_json": doc_json, "synthesis": doc_json}
    refined = research.refine_research_artifact(artifact, instruction)
    if refined is None:
        raise HTTPException(status_code=502, detail="Research refinement unavailable")
    new_id = bq.insert_research_artifact(refined)
    return {
        "artifact_id": new_id,
        "parent_artifact_id": aid,
        "doc": {
            "json": refined.get("doc_json"),
            "markdown": refined.get("doc_markdown"),
        },
        "refined": refined.get("refined", 1),
    }


# Reviewer sign in. The client id the page hands the identity provider is the
# audience the review credential is verified against, so it comes from the same
# configuration, and only while review is fully configured. The page then loads
# the provider's sign in script, and its policy allows exactly the provider
# origins that script needs. Unconfigured, the page and policy are as shipped.
_REVIEW_SIGNIN_SCRIPT = '<script src="https://accounts.google.com/gsi/client" async></script>'
REVIEW_SIGNIN_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/client; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://accounts.google.com/gsi/style; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data:; "
    "connect-src 'self' https://accounts.google.com/gsi/; "
    "frame-src https://accounts.google.com/gsi/; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


def _index_page(page: str, headers: dict) -> HTMLResponse:
    import html

    configuration = _review_configuration()
    if configuration is None or "</head>" not in page:
        return HTMLResponse(page, headers=headers)
    client_id = html.escape(configuration["audience"], quote=True)
    signin = (
        f'<meta name="review-oauth-client-id" content="{client_id}" />\n'
        f"  {_REVIEW_SIGNIN_SCRIPT}\n"
    )
    page = page.replace("</head>", signin + "</head>", 1)
    return HTMLResponse(
        page, headers={**headers, "Content-Security-Policy": REVIEW_SIGNIN_CSP}
    )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    if WEB_DIST_INDEX.exists():
        return _index_page(WEB_DIST_INDEX.read_text(encoding="utf-8"), headers)
    if WEB_INDEX.exists():
        return _index_page(WEB_INDEX.read_text(encoding="utf-8"), headers)
    return HTMLResponse(PLACEHOLDER_HTML, headers=headers)
