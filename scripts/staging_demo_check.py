from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit


DEFAULT_BASE_URL = "https://f42-api-590353929363.us-central1.run.app"
ALLOWED_ORIGINS = frozenset({DEFAULT_BASE_URL})
DEFAULT_OUTPUT_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs")
DEFAULT_STORAGE_STATE = Path("C:/Users/AlbertMeintjes/dev/42-secrets/l4-state.json")
SAST = timezone(timedelta(hours=2), "SAST")
VERSION_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
ASK_EVENTS_RE = re.compile(r"^/api/ask/[^/]+/events/?$")
ENCODED_PATH_SEPARATOR_RE = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)
MARKET_ASSUMED_RE = re.compile(r"Market assumed: (South Africa|Nigeria|Kenya)")
ASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MARKET_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
LONG_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
PARTIAL_MARKETS_PREFIX = "Some markets are incomplete: "
PARTIAL_EXPLANATION_BANNER = "Some trends are shown without an explanation yet."
SEARCHING_NOW_CAPTION = "Google search interest, not posts"
EARLIER_WARNING = "This is not today’s brief."
HISTORY_LINK_TEXT = "Choose a past brief in History"
HISTORY_HREF = "#/history"
TODAY_WAIT_MS = 45000
MAX_DISCLOSURES = 30
CHROME_PATH = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")

CHECK_NAMES = (
    "Staging health and version",
    "Today date and stale warning",
    "Today empty or partial market reason and History link",
    "Searching now caption",
    "Saved answer opens without rerun",
    "Inline citations expand",
    "Market assumed source detail",
    "Network write and external boundary",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    kind: str = "observed"


@dataclass(frozen=True)
class HealthObservation:
    status_code: int | None
    redirected: bool
    body: dict | None
    raw_body: str


def _check(name: str, passed: bool, detail: str, kind: str = "ui_failure") -> CheckResult:
    return CheckResult(name, "PASS" if passed else "FAIL", detail, "observed" if passed else kind)


def _missing(name: str, detail: str) -> CheckResult:
    return CheckResult(name, "FAIL", "Unverified: " + detail, "missing_case")


def validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("Use only the approved staging origin.")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise ValueError("Use only the approved staging origin.") from exc
    if (
        parsed.scheme.lower() != "https"
        or hostname not in {urlsplit(origin).hostname for origin in ALLOWED_ORIGINS}
        or parsed.netloc.lower() != hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use only the approved staging origin.")
    return DEFAULT_BASE_URL


def canonical_request_path(path: str) -> str:
    canonical = path
    for _ in range(8):
        decoded = unquote(canonical)
        if decoded == canonical:
            break
        canonical = decoded
    return canonical.replace("\\", "/")


def is_ask_event_path(path: str) -> bool:
    return bool(ASK_EVENTS_RE.fullmatch(canonical_request_path(path)))


def allows_request(method: str, url: str, allowed_origin: str) -> bool:
    try:
        parsed = urlsplit(url)
        allowed = urlsplit(allowed_origin)
        if parsed.username is not None or parsed.password is not None:
            return False
        same_origin = (
            parsed.scheme.lower() == "https"
            and parsed.netloc.lower() == allowed.netloc.lower()
            and allowed.scheme.lower() == "https"
        )
    except ValueError:
        return False
    if method.upper() not in {"GET", "HEAD"} or not same_origin:
        return False
    return not ENCODED_PATH_SEPARATOR_RE.search(parsed.path) and not is_ask_event_path(parsed.path)


def request_health(base_url: str, client=None) -> HealthObservation:
    owned_client = client is None
    try:
        if owned_client:
            import httpx

            client = httpx.Client(follow_redirects=False, timeout=10.0)
        response = client.get(
            base_url + "/api/health",
            follow_redirects=False,
            timeout=10.0,
        )
        status = int(response.status_code)
        redirected = 300 <= status < 400 or bool(getattr(response, "history", ()))
        try:
            body = response.json() if not redirected else None
        except Exception:
            body = None
        try:
            raw_body = response.text
        except Exception:
            raw_body = ""
        return HealthObservation(status, redirected, body if isinstance(body, dict) else None, raw_body)
    except Exception:
        return HealthObservation(None, False, None, "")
    finally:
        if owned_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


def versions_match(observed: str, expected: str | None) -> bool:
    if not isinstance(observed, str) or not VERSION_RE.fullmatch(observed):
        return False
    if expected is None:
        return True
    if not isinstance(expected, str) or not VERSION_RE.fullmatch(expected):
        return False
    actual = observed.lower()
    wanted = expected.lower()
    return actual.startswith(wanted) or wanted.startswith(actual)


def evaluate_health(observation: HealthObservation, expected_version: str | None = None) -> CheckResult:
    name = CHECK_NAMES[0]
    if observation is None or observation.status_code != 200 or observation.redirected:
        return _check(name, False, "Public health GET did not return HTTP 200 without a redirect.", "service_failure")
    body = observation.body
    if not isinstance(body, dict):
        return _check(name, False, "Public health response was not a JSON object.", "service_failure")
    version = body.get("version")
    if body.get("service") != "f42-api" or body.get("ok") is not True:
        return _check(name, False, "Health did not identify a ready f42-api service.", "service_failure")
    if not versions_match(version, expected_version):
        return _check(name, False, "Health version is missing, non-hexadecimal, or does not match the expected revision.", "service_failure")
    if body.get("auth_mode") != "passcode" or body.get("passcode") is not True:
        return _check(name, False, "Health does not confirm the passcode gate needed for the read only browser session.", "precondition")
    checks = body.get("checks")
    if not isinstance(checks, dict) or any(checks.get(key) != "ok" for key in ("auth", "bigquery", "agent")):
        return _check(name, False, "Health dependency checks are not ready.", "service_failure")
    revision_note = (
        " and matches the expected revision."
        if expected_version
        else "; no expected revision was supplied."
    )
    return _check(name, True, "Health is ready at version " + version + revision_note)


def format_long_date(value: date) -> str:
    return f"{value.day} {LONG_MONTHS[value.month - 1]} {value.year}"


def evaluate_today_date(
    brief_date: str | None,
    today: date,
    warning_visible: bool,
    date_context_text: str,
    warning_text_ok: bool = True,
) -> CheckResult:
    name = CHECK_NAMES[1]
    try:
        parsed = date.fromisoformat(brief_date or "")
    except (TypeError, ValueError):
        return _missing(name, "the live Today response did not include a usable brief date.")
    if parsed.isoformat() != brief_date:
        return _check(name, False, "The live Today date was not in YYYY-MM-DD form.", "service_failure")
    if parsed > today:
        return _check(name, False, "The live Today brief date is ahead of the SAST date.", "service_failure")
    expects_warning = parsed < today
    if warning_visible != expects_warning or (expects_warning and not warning_text_ok):
        return _check(name, False, "The not-today warning did not match the live brief date.", "ui_failure")
    expected_context = "Brief for " + format_long_date(parsed)
    if expected_context not in (date_context_text or ""):
        return _check(name, False, "The explicit date route did not show the live brief date.", "ui_failure")
    return _check(name, True, "The visible brief date and not-today warning matched the SAST date.")


def expected_partial_banner(payload: dict | None) -> str | None:
    """Mirror the partial banner wording that today42.jsx renders for this payload."""
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, list):
        return None
    incomplete = []
    for market in markets:
        if not isinstance(market, dict) or market.get("status") == "published":
            continue
        label = market.get("label")
        incomplete.append(label if isinstance(label, str) and label.strip() else str(market.get("market")))
    if not incomplete:
        return PARTIAL_EXPLANATION_BANNER
    return PARTIAL_MARKETS_PREFIX + ", ".join(incomplete) + "."


def evaluate_today_availability(
    markets: list[dict],
    payload_status: str | None,
    banner_text: str,
    *,
    expected_banner: str | None = None,
    page_history: dict | None = None,
    all_markets_empty: bool = False,
) -> CheckResult:
    name = CHECK_NAMES[2]
    has_partial = payload_status == "partial"
    if not markets:
        return _missing(name, "no affected market with a visible reason and History link was present.")
    page_link_shown = bool(page_history and page_history.get("visible"))
    for market in markets:
        if not market.get("reason_visible"):
            return _check(name, False, "An observed empty market lacked its plain reason.", "ui_failure")
    if all_markets_empty:
        if not page_link_shown or page_history.get("href") != HISTORY_HREF:
            return _check(name, False, "Every market was empty but the page did not show its one History link.", "ui_failure")
        if any(market.get("history_visible") for market in markets):
            return _check(name, False, "Every market was empty but History links were repeated per market.", "ui_failure")
    else:
        if page_link_shown:
            return _check(name, False, "The page-level History link showed while some market had cards.", "ui_failure")
        for market in markets:
            if not market.get("history_visible") or market.get("history_href") != HISTORY_HREF:
                return _check(name, False, "An observed empty market lacked its History link.", "ui_failure")
    if has_partial and (expected_banner is None or banner_text != expected_banner):
        return _check(name, False, "The live partial brief did not show the required caution text.", "ui_failure")
    return _check(name, True, "An affected live market showed its reason and History path.")


def evaluate_searching_now(
    payload_count: int | None,
    section_present: bool,
    caption_text: str | list[str],
) -> CheckResult:
    name = CHECK_NAMES[3]
    if payload_count is None:
        return _missing(name, "the live Today payload did not expose a search row case.")
    if payload_count == 0:
        if section_present:
            return _check(name, False, "The live empty search payload still rendered a Searching now strip.", "ui_failure")
        return _missing(name, "no populated rows were available; the strip was hidden, so the caption remains unverified.")
    captions = [caption_text] if isinstance(caption_text, str) else caption_text
    passed = section_present and bool(captions) and all(text == SEARCHING_NOW_CAPTION for text in captions)
    return _check(name, passed, "The live populated search strip did not show its exact caption.", "ui_failure")


def evaluate_saved_answer(ask_id: str | None, article_visible: bool, requests: list[dict]) -> CheckResult:
    name = CHECK_NAMES[4]
    if not ask_id:
        return _missing(name, "History had no live saved answer to open.")
    detail_path = "/api/ask/" + quote(ask_id, safe="")
    detail_reads = [
        request for request in requests
        if request.get("path") == detail_path and request.get("method") == "GET"
    ]
    writes = [
        request for request in requests
        if request.get("path") == "/api/ask" and request.get("method") != "GET"
    ]
    event_reads = [request for request in requests if is_ask_event_path(request.get("path", ""))]
    if not article_visible and not detail_reads:
        return _check(name, False, "The saved answer did not open from its live History row.", "ui_failure")
    if len(detail_reads) != 1 or writes or event_reads or not article_visible:
        return _check(name, False, "The saved answer did not open with one detail GET and no Ask write or event stream.", "ui_failure")
    return _check(name, True, "The saved answer opened from one detail GET without an Ask write or event stream.")


def evaluate_citations(
    toggle_found: bool,
    initially_collapsed: bool,
    expanded: bool,
    fewer_label: str,
    before_count: int,
    after_count: int,
) -> CheckResult:
    name = CHECK_NAMES[5]
    if not toggle_found:
        return _missing(name, "the live saved answer had no claim with multiple cited sources.")
    passed = initially_collapsed and expanded and fewer_label == "Show fewer sources" and after_count > before_count
    if not passed:
        return _check(name, False, "The live answer citation disclosure did not expand inline and expose its Fewer control.", "ui_failure")
    return _check(name, True, "The live answer expanded cited sources inline and exposed its Fewer control.")


def expected_market_assumed_labels(saved_answer: dict | None) -> list[str]:
    answer = saved_answer.get("answer") if isinstance(saved_answer, dict) else None
    evidence = answer.get("evidence") if isinstance(answer, dict) else None
    if not isinstance(evidence, list):
        return []
    labels = {
        "Market assumed: " + MARKET_NAMES[item["source_market"]]
        for item in evidence
        if isinstance(item, dict)
        and isinstance(item.get("flags"), list)
        and "market_assumed" in item["flags"]
        and item.get("source_market") in MARKET_NAMES
    }
    return sorted(labels)


def evaluate_market_assumed(expected_labels: list[str], observed_panel_labels: list[str]) -> CheckResult:
    name = CHECK_NAMES[6]
    if not expected_labels:
        return _missing(name, "the live saved answer had no source flagged with market_assumed.")
    if not set(expected_labels).issubset(observed_panel_labels):
        return _check(name, False, "The flagged live source detail did not show its Market assumed label.", "ui_failure")
    return _check(name, True, "API flagged source details showed their Market assumed labels.")


def redact_secret(value: str, secret: str | None) -> str:
    text = str(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    return text


def _safe_metadata(metadata: dict | None) -> list[str]:
    values = metadata if isinstance(metadata, dict) else {}
    brief_date = values.get("brief_date")
    try:
        parsed_date = date.fromisoformat(brief_date or "")
        safe_date = parsed_date.isoformat() if parsed_date.isoformat() == brief_date else "unobserved"
    except (TypeError, ValueError):
        safe_date = "unobserved"
    ask_id = values.get("saved_ask_id")
    safe_ask_id = ask_id if isinstance(ask_id, str) and ASK_ID_RE.fullmatch(ask_id) else "unobserved"
    cards = values.get("cards_by_market")
    card_values = []
    for market in ("ZA", "NG", "KE"):
        count = cards.get(market) if isinstance(cards, dict) else None
        safe_count = str(count) if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100000 else "unobserved"
        card_values.append(market + "=" + safe_count)
    seconds = values.get("today_load_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds <= 3600:
        load_time = (f"{seconds:.1f} s" if values.get("today_loaded") is True
                     else f"not loaded after {seconds:.1f} s")
    else:
        load_time = "unobserved"
    return [
        "SAST brief date: " + safe_date,
        "Saved Ask id: " + safe_ask_id,
        "Cards by market: " + ", ".join(card_values),
        "Today load time: " + load_time,
    ]


def render_report(
    checks: list[CheckResult],
    now: datetime,
    secret: str | None = None,
    metadata: dict | None = None,
) -> str:
    local_time = now.astimezone(SAST) if now.tzinfo else now.replace(tzinfo=SAST)
    lines = [
        "42 staging demo check",
        "Run time: " + local_time.strftime("%Y-%m-%d %H:%M SAST"),
        "Origin: approved staging service",
        "",
    ]
    lines.extend(f"{item.status} | {item.name} | {item.kind} | {item.detail}" for item in checks)
    lines.extend(["", *_safe_metadata(metadata)])
    return redact_secret("\n".join(lines) + "\n", secret)


def write_report(
    output_dir: str | Path,
    checks: list[CheckResult],
    now: datetime | None = None,
    secret: str | None = None,
    metadata: dict | None = None,
) -> Path:
    timestamp = now or datetime.now(SAST)
    local_time = timestamp.astimezone(SAST) if timestamp.tzinfo else timestamp.replace(tzinfo=SAST)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = "STAGING-CHECK-" + local_time.strftime("%H%M")
    report_text = render_report(checks, local_time, secret, metadata)
    path = directory / f"{stem}.md"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(report_text)
    return path


def _check_map(reason: str, kind: str = "precondition") -> dict[str, CheckResult]:
    return {name: CheckResult(name, "FAIL", reason, kind) for name in CHECK_NAMES}


def _auth_expired_results(boundary_ready: bool = False) -> dict[str, CheckResult]:
    detail = "AUTH_EXPIRED: human unlock is required. No login attempt was made."
    results = {
        name: CheckResult(name, "FAIL", detail, "precondition")
        for name in CHECK_NAMES[1:]
    }
    if boundary_ready:
        results[CHECK_NAMES[-1]] = _check(
            CHECK_NAMES[-1],
            True,
            "The read only request boundary remained enforced after session expiry.",
        )
    return results


def _today_payload(response_map: dict, requested_date: str | None = None) -> dict | None:
    candidates = response_map.get("today", [])
    for query_date, payload in candidates:
        if query_date == requested_date:
            return payload
    return None


def _date_query(response_url: str) -> str | None:
    values = parse_qs(urlsplit(response_url).query).get("date", [])
    return values[0] if len(values) == 1 else None


def ask_follow_id(href: str) -> str | None:
    fragment = urlsplit(href).fragment
    route, separator, query = fragment.partition("?")
    if route != "/ask" or not separator:
        return None
    try:
        parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if set(parameters) != {"follow"}:
        return None
    values = parameters["follow"]
    ask_id = values[0] if len(values) == 1 else None
    return ask_id if isinstance(ask_id, str) and ASK_ID_RE.fullmatch(ask_id) else None


def install_websocket_guard(context, handler) -> bool:
    route = getattr(context, "route_web_socket", None)
    if not callable(route):
        return False
    try:
        route("**/*", handler)
        return True
    except Exception:
        return False


def _run_browser_checks(
    base_url: str,
    storage_state_path: Path,
    health: HealthObservation,
    sast_today: date,
    *,
    playwright_factory=None,
) -> dict[str, CheckResult]:
    for name in ("DEBUG", "PWDEBUG", "PWTEST_DEBUG", "PLAYWRIGHT_DEBUG"):
        os.environ.pop(name, None)
    logging.disable(logging.DEBUG)
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright

    observations = {
        "api_requests": [],
        "today_responses": [],
        "external_blocked": 0,
        "non_read_blocked": 0,
        "ask_events_blocked": 0,
        "websocket_attempts": 0,
        "guard_failed": False,
    }
    health_raw = health.raw_body or json.dumps(health.body or {})
    checks: dict[str, CheckResult] = {}
    metadata = {
        "brief_date": None,
        "saved_ask_id": None,
        "cards_by_market": {},
        "today_load_seconds": None,
        "today_loaded": False,
    }

    with playwright_factory() as playwright:
        launch = {"headless": True}
        if CHROME_PATH.is_file():
            launch["executable_path"] = str(CHROME_PATH)
        browser = playwright.chromium.launch(**launch)
        try:
            context = browser.new_context(
                storage_state=str(storage_state_path),
                viewport={"width": 1280, "height": 900},
                service_workers="block",
            )
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            return _auth_expired_results()

        def close_socket(socket):
            observations["websocket_attempts"] += 1
            try:
                socket.close()
            except Exception:
                observations["guard_failed"] = True

        def pre_navigation_failure(detail):
            failed = {name: _missing(name, "browser checks did not start because the network boundary was unavailable.") for name in CHECK_NAMES}
            failed[CHECK_NAMES[-1]] = _check(CHECK_NAMES[-1], False, detail, "precondition")
            failed["_metadata"] = metadata
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            return failed

        if not install_websocket_guard(context, close_socket):
            return pre_navigation_failure("WebSocket routing is unavailable; navigation was refused.")

        def guard(route):
            request = route.request
            parsed = urlsplit(request.url)
            method = request.method.upper()
            path = canonical_request_path(parsed.path)
            same_host = parsed.scheme.lower() == "https" and parsed.netloc.lower() == urlsplit(base_url).netloc.lower()
            if same_host and path.startswith("/api/"):
                observations["api_requests"].append({"method": method, "path": path})
            if is_ask_event_path(path):
                observations["ask_events_blocked"] += 1
            if observations.get("auth_expired") and path.startswith("/api/"):
                try:
                    route.abort("blockedbyclient")
                except Exception:
                    observations["guard_failed"] = True
                return
            allowed = allows_request(method, request.url, base_url)
            if not allowed:
                if not same_host:
                    observations["external_blocked"] += 1
                if method not in {"GET", "HEAD"}:
                    observations["non_read_blocked"] += 1
                try:
                    route.abort("blockedbyclient")
                except Exception:
                    observations["guard_failed"] = True
                return
            if path == "/api/health":
                try:
                    route.fulfill(status=200, content_type="application/json", body=health_raw)
                except Exception:
                    observations["guard_failed"] = True
                return
            route.continue_()

        def record_response(response):
            try:
                parsed = urlsplit(response.url)
                path = canonical_request_path(parsed.path)
                same_origin = parsed.scheme.lower() == "https" and parsed.netloc.lower() == urlsplit(base_url).netloc.lower()
                method = response.request.method.upper()
                if same_origin and parsed.path.startswith("/api/") and response.status in {401, 403}:
                    observations["auth_expired"] = True
                    return
                if not same_origin or method != "GET" or response.status != 200:
                    return
                payload = response.json()
                if isinstance(payload, dict):
                    if path == "/api/today":
                        observations["today_responses"].append((_date_query(response.url), payload))
                    elif path == "/api/history/asks":
                        observations["history_asks"] = payload
                    elif re.fullmatch(r"/api/ask/[^/]+", path):
                        observations.setdefault("saved_answers", {})[path] = payload
            except Exception:
                return

        try:
            context.route("**/*", guard)
        except Exception:
            return pre_navigation_failure("The HTTP request boundary could not be installed; navigation was refused.")

        page = context.new_page()
        page.set_default_timeout(12000)
        page.on("response", record_response)

        today_main = None
        latest = None
        source_payload = None
        try:
            today_started = time.monotonic()
            try:
                page.goto(base_url + "/#/today", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => document.querySelector('#main-content [data-today-loaded]') "
                    + "|| document.querySelector('[data-screen-label=\"Passcode gate\"]')",
                    timeout=TODAY_WAIT_MS,
                )
            finally:
                metadata["today_load_seconds"] = round(time.monotonic() - today_started, 1)
            if page.locator('[data-screen-label="Passcode gate"]').count():
                observations["auth_expired"] = True
                raise RuntimeError("auth gate")
            today_main = page.locator("#main-content")
            today_main.locator("[data-today-loaded]").wait_for(state="visible")
            metadata["today_loaded"] = True
            latest = _today_payload({"today": observations["today_responses"]})
            brief_date = latest.get("date") if isinstance(latest, dict) else None
            warning = today_main.locator("[data-today-earlier]")
            warning_visible = warning.count() > 0 and warning.is_visible()
            warning_text_ok = warning_visible and EARLIER_WARNING in warning.inner_text()
            valid_date = False
            try:
                parsed = date.fromisoformat(brief_date or "")
                valid_date = parsed.isoformat() == brief_date
            except (TypeError, ValueError):
                pass
            if valid_date:
                metadata["brief_date"] = brief_date
                context_text = ""
                try:
                    page.goto(base_url + "/#/today?date=" + quote(brief_date, safe="-"), wait_until="domcontentloaded")
                    today_main = page.locator("#main-content")
                    today_main.locator("[data-today-loaded]").wait_for(state="visible", timeout=TODAY_WAIT_MS)
                    date_context = today_main.locator("[data-today-date-context]")
                    context_text = date_context.inner_text() if date_context.count() and date_context.is_visible() else ""
                    source_payload = _today_payload({"today": observations["today_responses"]}, brief_date) or latest
                except Exception:
                    checks[CHECK_NAMES[1]] = _check(CHECK_NAMES[1], False, "The explicit date route did not load the live brief.", "ui_failure")
                    source_payload = latest
                if CHECK_NAMES[1] not in checks:
                    checks[CHECK_NAMES[1]] = evaluate_today_date(
                        brief_date, sast_today, warning_visible, context_text, warning_text_ok
                    )
            else:
                checks[CHECK_NAMES[1]] = evaluate_today_date(brief_date, sast_today, warning_visible, "", warning_text_ok)
                source_payload = latest
        except Exception:
            checks[CHECK_NAMES[1]] = _check(CHECK_NAMES[1], False, "The live Today route did not load its date state.", "ui_failure")

        if observations.get("auth_expired"):
            auth_results = _auth_expired_results(boundary_ready=not observations["guard_failed"])
            auth_results["_metadata"] = metadata
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            return auth_results

        if today_main is not None:
            try:
                all_tab = today_main.get_by_role("tab", name="All", exact=True)
                all_scope = all_tab.count() > 0 and all_tab.is_visible()
                if all_scope:
                    all_tab.click()
                    all_scope = all_tab.get_attribute("aria-selected") == "true"
                empty_markets = []
                cards_by_market = {}
                markets = today_main.locator(".t42-market[data-market]")
                for index in range(markets.count()):
                    market = markets.nth(index)
                    code = market.get_attribute("data-market") or ""
                    card_count = market.locator("[data-card]").count()
                    if code in MARKET_NAMES:
                        cards_by_market[code] = card_count
                    if card_count:
                        continue
                    reason = market.locator("[data-empty-market-reason]")
                    history = market.get_by_role("link", name=HISTORY_LINK_TEXT, exact=True)
                    empty_markets.append({
                        "reason_visible": reason.count() > 0 and reason.is_visible() and bool(reason.inner_text().strip()),
                        "history_visible": history.count() > 0 and history.is_visible(),
                        "history_href": history.get_attribute("href") if history.count() else None,
                    })
                metadata["cards_by_market"] = {code: cards_by_market.get(code) for code in MARKET_NAMES} if all_scope else {}
                page_link = today_main.locator("[data-today-history-link]").get_by_role(
                    "link", name=HISTORY_LINK_TEXT, exact=True
                )
                page_history = {
                    "visible": page_link.count() > 0 and page_link.first.is_visible(),
                    "href": page_link.first.get_attribute("href") if page_link.count() else None,
                }
                all_markets_empty = markets.count() > 0 and len(empty_markets) == markets.count()
                partial = today_main.locator('[data-today-status="partial"]')
                partial_text = partial.inner_text().strip() if partial.count() and partial.is_visible() else ""
                payload_status = source_payload.get("status") if isinstance(source_payload, dict) else None
                checks[CHECK_NAMES[2]] = (
                    evaluate_today_availability(
                        empty_markets,
                        payload_status,
                        partial_text,
                        expected_banner=expected_partial_banner(source_payload),
                        page_history=page_history,
                        all_markets_empty=all_markets_empty,
                    )
                    if all_scope
                    else _check(CHECK_NAMES[2], False, "The All market view was unavailable for this live brief.", "ui_failure")
                )
            except Exception:
                checks[CHECK_NAMES[2]] = _check(CHECK_NAMES[2], False, "The live market state could not be read.", "ui_failure")
        else:
            checks[CHECK_NAMES[2]] = _check(CHECK_NAMES[2], False, "The Today market state did not load.", "ui_failure")

        if today_main is not None:
            try:
                search_rows = source_payload.get("searching_now") if isinstance(source_payload, dict) else None
                search_count = len(search_rows) if isinstance(search_rows, list) else None
                strips = today_main.locator('[data-section="searching-now"]')
                visible_strips = [strips.nth(i) for i in range(strips.count()) if strips.nth(i).is_visible()]
                captions = []
                for strip in visible_strips:
                    caption = strip.locator(".searching-now__caption")
                    captions.append(caption.inner_text().strip() if caption.count() and caption.is_visible() else "")
                checks[CHECK_NAMES[3]] = evaluate_searching_now(search_count, bool(visible_strips), captions)
            except Exception:
                checks[CHECK_NAMES[3]] = _check(CHECK_NAMES[3], False, "The live Searching now strip could not be read.", "ui_failure")
        else:
            checks[CHECK_NAMES[3]] = _check(CHECK_NAMES[3], False, "The Today search state did not load.", "ui_failure")

        history_loaded = False
        history_main = None
        try:
            with page.expect_response(
                lambda item: urlsplit(item.url).path == "/api/history/asks" and item.request.method.upper() == "GET",
                timeout=8000,
            ) as history_response_info:
                page.goto(base_url + "/#/history", wait_until="domcontentloaded")
            history_main = page.locator("#main-content")
            history_main.get_by_role("heading", name="History", exact=True).wait_for(state="visible")
            response = history_response_info.value
            history_payload = response.json() if response.status == 200 else None
            observations["history_asks"] = history_payload if isinstance(history_payload, dict) else None
            history_loaded = isinstance(observations["history_asks"], dict)
        except Exception:
            observations["history_asks"] = None

        ask_id = None
        article = None
        article_visible = False
        saved_payload = None
        if history_loaded:
            asks = observations["history_asks"].get("asks")
            eligible = [
                item for item in asks if isinstance(item, dict)
                and item.get("status") in {"complete", "ok"}
                and item.get("answer_status") in {"complete", "partial"}
                and isinstance(item.get("ask_id"), str)
                and ASK_ID_RE.fullmatch(item["ask_id"])
            ] if isinstance(asks, list) else []
            if eligible:
                read_answer = history_main.get_by_role("link", name="Read answer", exact=True).first
                try:
                    read_answer.wait_for(state="visible", timeout=6000)
                    href = read_answer.get_attribute("href") or ""
                    ask_id = ask_follow_id(href)
                    if ask_id in {item["ask_id"] for item in eligible}:
                        metadata["saved_ask_id"] = ask_id
                        detail_path = "/api/ask/" + quote(ask_id, safe="")
                        with page.expect_response(
                            lambda item: urlsplit(item.url).path == detail_path and item.request.method.upper() == "GET",
                            timeout=8000,
                        ) as detail_info:
                            read_answer.click()
                        detail_response = detail_info.value
                        try:
                            saved_payload = detail_response.json() if detail_response.status == 200 else None
                        except Exception:
                            saved_payload = None
                        article = page.locator("#main-content").get_by_role("article").first
                        try:
                            article.wait_for(state="visible", timeout=6000)
                            article_visible = True
                        except Exception:
                            article_visible = False
                    else:
                        ask_id = None
                except Exception:
                    article_visible = False
            checks[CHECK_NAMES[4]] = evaluate_saved_answer(ask_id, article_visible, observations["api_requests"])
        else:
            checks[CHECK_NAMES[4]] = _check(CHECK_NAMES[4], False, "The History list did not finish its bounded live read.", "ui_failure")

        if observations.get("auth_expired"):
            detail = "AUTH_EXPIRED: human unlock is required. No login attempt was made."
            for name in (CHECK_NAMES[4], CHECK_NAMES[5], CHECK_NAMES[6]):
                checks[name] = CheckResult(name, "FAIL", detail, "precondition")
            checks[CHECK_NAMES[7]] = _check(
                CHECK_NAMES[7],
                not observations["guard_failed"],
                "The read only request boundary remained enforced after session expiry.",
                "precondition",
            )
            checks["_metadata"] = metadata
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            return checks

        if article_visible and article is not None:
            try:
                disclosures = article.locator("button.ask42-chip-more")
                if disclosures.count():
                    disclosure = disclosures.first
                    initially_collapsed = disclosure.get_attribute("aria-expanded") == "false"
                    before_count = article.locator('button[aria-label^="Source:"]').count()
                    disclosure.click()
                    expanded = disclosure.get_attribute("aria-expanded") == "true"
                    fewer_label = disclosure.get_attribute("aria-label") or ""
                    after_count = article.locator('button[aria-label^="Source:"]').count()
                    checks[CHECK_NAMES[5]] = evaluate_citations(True, initially_collapsed, expanded, fewer_label, before_count, after_count)
                    disclosure.click()
                else:
                    checks[CHECK_NAMES[5]] = evaluate_citations(False, False, False, "", 0, 0)
            except Exception:
                checks[CHECK_NAMES[5]] = _check(CHECK_NAMES[5], False, "The live citation disclosure did not open inline.", "ui_failure")

            expected_labels = expected_market_assumed_labels(saved_payload)
            observed_labels = []
            try:
                disclosures = article.locator("button.ask42-chip-more")
                if disclosures.count() > MAX_DISCLOSURES:
                    checks[CHECK_NAMES[6]] = _missing(CHECK_NAMES[6], "the saved answer exceeded the bounded source expansion scan.")
                else:
                    for index in range(disclosures.count()):
                        disclosure = disclosures.nth(index)
                        if disclosure.get_attribute("aria-expanded") == "false":
                            disclosure.click()
                    sources = article.locator('button[aria-label^="Source:"]')
                    for expected_label in expected_labels:
                        for index in range(sources.count()):
                            source = sources.nth(index)
                            if expected_label not in (source.get_attribute("aria-label") or ""):
                                continue
                            source.click()
                            panel = page.locator('#main-content aside[aria-label="Source"]')
                            if panel.count() and panel.is_visible() and expected_label in panel.inner_text():
                                observed_labels.append(expected_label)
                            break
                    checks[CHECK_NAMES[6]] = evaluate_market_assumed(expected_labels, observed_labels)
            except Exception:
                checks[CHECK_NAMES[6]] = _check(CHECK_NAMES[6], False, "The flagged source detail could not be verified.", "ui_failure")
        else:
            checks[CHECK_NAMES[5]] = _missing(CHECK_NAMES[5], "the live saved answer had no visible citation disclosure case.")
            checks[CHECK_NAMES[6]] = evaluate_market_assumed(expected_market_assumed_labels(saved_payload), [])

        checks[CHECK_NAMES[7]] = _check(
            CHECK_NAMES[7],
            not observations["guard_failed"],
            "Nonread, cross origin, Ask event, and WebSocket requests are intercepted before leaving the browser.",
            "precondition",
        )
        checks["_metadata"] = metadata

        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        return checks


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("Invalid checker arguments.")


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(description="Read only browser checks for the approved 42 staging service.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--expected-version")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--storage-state", type=Path, default=DEFAULT_STORAGE_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    health_fetcher=None,
    browser_runner=None,
    clock=None,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except ValueError:
        print("Invalid checker arguments.", file=sys.stderr)
        return 2
    if args.workers != 1:
        print("Only one browser worker is supported.", file=sys.stderr)
        return 2

    now = (clock or (lambda: datetime.now(SAST)))()
    local_now = now.astimezone(SAST) if now.tzinfo else now.replace(tzinfo=SAST)
    checks = _check_map("Precondition failed; no staging checks were run.")
    metadata = {}

    try:
        base_url = validate_base_url(args.base_url)
    except ValueError:
        checks = _check_map("Configured URL is not the approved staging origin.")
    else:
        expected_version = args.expected_version
        if expected_version is not None and not VERSION_RE.fullmatch(expected_version):
            checks = _check_map("Expected revision must be a hexadecimal build id.")
        else:
            try:
                health = (health_fetcher or request_health)(base_url)
            except Exception:
                health = HealthObservation(None, False, None, "")
            health_check = evaluate_health(health, expected_version)
            checks[CHECK_NAMES[0]] = health_check
            if health_check.status == "PASS":
                if not args.storage_state.is_file():
                    for name, result in _auth_expired_results().items():
                        checks[name] = result
                elif browser_runner is None and importlib.util.find_spec("playwright") is None:
                    for name in CHECK_NAMES[1:]:
                        checks[name] = _check(name, False, "Python Playwright is not installed.", "precondition")
                else:
                    try:
                        observed = (browser_runner or _run_browser_checks)(
                            base_url,
                            args.storage_state,
                            health,
                            local_now.date(),
                        )
                    except Exception:
                        observed = {}
                    if not isinstance(observed, dict):
                        observed = {}
                    metadata = observed.get("_metadata", {})
                    for name in CHECK_NAMES[1:]:
                        checks[name] = observed.get(name) or _missing(name, "the browser could not verify this live case.")
            else:
                for name in CHECK_NAMES[1:]:
                    checks[name] = CheckResult(name, "FAIL", "Live health precondition failed; this screen was not checked.", "precondition")

    ordered = [checks[name] for name in CHECK_NAMES]
    try:
        report = write_report(args.output_dir, ordered, now=local_now, metadata=metadata)
    except Exception:
        print("Could not write the staging report.", file=sys.stderr)
        return 1
    print("Report written: " + str(report))
    return 0 if all(item.status == "PASS" for item in ordered) else 1


if __name__ == "__main__":
    raise SystemExit(main())
