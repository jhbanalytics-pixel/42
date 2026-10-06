#!/usr/bin/env python3
"""Deep QA of the live 42 tool, for the daily morning-check and for
continuous improvement.

Probes the open health endpoint, then the passcode-gated data endpoints, times
each call, checks the data is fresh against the engine's trend_date, and writes
a dated line to docs/qa_log.md so regressions and drift are visible over time.
This is the API + data + freshness + latency layer. The UI render / console /
lighthouse layer runs from morning-check via chrome-devtools (see SKILL.md);
this script is the part that runs headless and logs a trend line every day.

Passcode: read from UI_PASSCODE, else pulled from the live Cloud Run service at
runtime (never printed). With no passcode the gated probes report SKIPPED, not
FAIL, so the script still runs in a bare environment.

Usage:
    python scripts/lp_qa.py [--base URL] [--json] [--no-log]
Exit code: 0 = HEALTHY, 1 = DEGRADED/DOWN (so morning-check can gate on it).
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_real_getaddrinfo = socket.getaddrinfo


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    """getaddrinfo with the IPv4 answers moved to the front.

    Cloud Run answers with 8 IPv6 addresses ahead of 8 IPv4 ones, and on this
    network no IPv6 address is reachable: they time out rather than refuse.
    socket.create_connection walks the list in order and spends the FULL socket
    timeout on each address before moving to the next, so a 30 second probe can
    burn 240 seconds on dead IPv6 before reaching an IPv4 address that connects
    in 0.02 seconds. Every latency number in qa_log.md is contaminated by that,
    and the 169 second health readings on 7, 10, 17 and 18 Aug 2026 are entirely
    it: /api/health returns a freshness stamp and cannot take three minutes to
    produce one. curl was never affected because it happy-eyeballs.

    Reordering rather than dropping IPv6 keeps this working on a network where
    IPv6 is the only path, which is why it is not a filter.
    """
    infos = _real_getaddrinfo(host, port, family, type, proto, flags)
    return sorted(infos, key=lambda i: i[0] != socket.AF_INET)


socket.getaddrinfo = _ipv4_first

BASE = "https://listening-post-590353929363.us-central1.run.app"
STAGING_BASE = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
REGION, PROJECT, SERVICE = "us-central1", "ogilvy-trends-v2", "listening-post"
STAGING_SERVICE = "listening-post-staging"
SLOW_MS = 2500  # a gated endpoint slower than this is a perf finding
LOG_PATH = Path(__file__).resolve().parents[1] / "docs" / "qa_log.md"

# Platforms whose absence from platform_heat is expected, so a daily FAIL on them
# is noise. platform_heat is a top-10 cut, so a platform lands here when it is
# ingested but too thin to rank. Empty since 4 Aug 2026: X was the only member and
# it climbed back into the top 10 the day after the handle lists were rebuilt on
# fresh-post count (24 X rows a day to 188), which is exactly the state change the
# regained-platform finding below exists to catch. Add a platform here only with a
# dated note saying why its absence is expected.
KNOWN_GAP_PLATFORMS: set[str] = set()

# Gated endpoints worth probing every day: the ones the board, desk, metrics and
# feed views depend on. Each is (label, path, timeout_seconds).
GATED = [
    ("today", "/api/today?market=all", 30),
    ("metrics", "/api/metrics", 30),
    ("rails", "/api/rails", 30),
    ("desk", "/api/desk?region=za", 30),
    ("voices", "/api/voices?region=all", 30),
    ("seed_discover", "/api/seed-discover?status=pending&market=ng", 30),
    ("seed_path", "/api/seed-path?keyword=amapiano&market=za", 30),
]


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _qa_destination(base: object, service: object) -> str:
    allowed = {BASE: SERVICE, STAGING_BASE: STAGING_SERVICE}
    if not isinstance(base, str) or not isinstance(service, str):
        raise ValueError("qa_destination_invalid")
    try:
        parsed = urllib.parse.urlsplit(base)
        port = parsed.port
    except ValueError as error:
        raise ValueError("qa_destination_invalid") from error
    normalized = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or allowed.get(normalized) != service
    ):
        raise ValueError("qa_destination_invalid")
    return normalized


def _google_api_destination(url: object) -> None:
    if not isinstance(url, str):
        raise ValueError("google_api_destination_invalid")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("google_api_destination_invalid") from error
    run = rf"/v2/projects/{re.escape(PROJECT)}/locations/{re.escape(REGION)}/services/[a-z][a-z0-9-]{{0,62}}"
    secret = rf"/v1/projects/{re.escape(PROJECT)}/secrets/[A-Za-z0-9_-]+/versions/(?:latest|[0-9]+):access"
    valid_path = (
        parsed.hostname == "run.googleapis.com"
        and re.fullmatch(run, parsed.path) is not None
    ) or (
        parsed.hostname == "secretmanager.googleapis.com"
        and re.fullmatch(secret, parsed.path) is not None
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise ValueError("google_api_destination_invalid")


def _api(url: str, token: str, timeout_s: int = 30) -> dict:
    """GET a Google API as JSON, over this module's IPv4-first resolver.

    gcloud does its own DNS and connection handling, so the reordering above
    does not reach it: `gcloud run services describe` hits its 120 second
    timeout and returns nothing on this network while the same read over urllib
    answers in about a second. The token is the one thing still worth asking
    gcloud for, because it is served from cached credentials without a network
    round trip.
    """
    _google_api_destination(url)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.build_opener(_NoCredentialRedirect()).open(
        req, timeout=timeout_s
    ) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _passcode(service: str = SERVICE) -> str:
    if service not in (SERVICE, STAGING_SERVICE):
        raise ValueError("qa_destination_invalid")
    pc = os.environ.get("UI_PASSCODE", "").strip()
    if pc:
        return pc
    try:
        # shell=True so Windows resolves gcloud.cmd; no untrusted input in the command.
        tok = subprocess.run(
            "gcloud auth print-access-token",
            capture_output=True,
            text=True,
            timeout=60,
            shell=True,
        )
        if tok.returncode != 0 or not tok.stdout.strip():
            return ""
        token = tok.stdout.strip()

        svc = _api(
            f"https://run.googleapis.com/v2/projects/{PROJECT}"
            f"/locations/{REGION}/services/{service}",
            token,
        )
        for c in svc.get("template", {}).get("containers", []):
            for e in c.get("env", []):
                if e.get("name") != "UI_PASSCODE":
                    continue
                if e.get("value"):
                    return e["value"]
                # Secret-backed env. The v2 API names the secret under
                # valueSource.secretKeyRef.secret, not valueFrom.secretKeyRef.name
                # as the v1 shape gcloud printed did.
                ref = e.get("valueSource", {}).get("secretKeyRef", {})
                if ref.get("secret"):
                    sec = _api(
                        f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}"
                        f"/secrets/{ref['secret']}"
                        f"/versions/{ref.get('version', 'latest')}:access",
                        token,
                    )
                    data = sec.get("payload", {}).get("data")
                    if data:
                        return base64.b64decode(data).decode("utf-8").strip()
    except (subprocess.SubprocessError, urllib.error.URLError, OSError):
        pass
    return ""


def _get(
    url: str, passcode: str = "", timeout_s: int = 30
) -> tuple[int, float, object]:
    req = urllib.request.Request(
        url, headers={"X-Passcode": passcode} if passcode else {}
    )
    t0 = time.monotonic()
    try:
        with urllib.request.build_opener(_NoCredentialRedirect()).open(
            req, timeout=timeout_s
        ) as r:
            body = r.read().decode("utf-8", "replace")
            ms = (time.monotonic() - t0) * 1000
            try:
                return r.status, ms, json.loads(body)
            except ValueError:
                return r.status, ms, body
    except urllib.error.HTTPError as e:
        return e.code, (time.monotonic() - t0) * 1000, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, (time.monotonic() - t0) * 1000, str(e)


def _payload_dates(obj: object) -> list[str]:
    """Every YYYY-MM-DD string anywhere in the payload."""
    return re.findall(
        r"\d{4}-\d{2}-\d{2}", json.dumps(obj) if not isinstance(obj, str) else obj
    )


# Keys whose value is a forward-looking LABEL rather than data. seed-discover ships
# week_end for the current ISO week, so it is future-dated on six days in seven,
# and warning on it every day is noise that buries the real finding. The exemption
# is scoped to the key: the same date under any other key still warns.
_FUTURE_LABEL_KEYS = frozenset({"week_end"})


def _keyed_dates(obj: object, key: str | None = None):
    """Yield (key, YYYY-MM-DD) for every date in the payload, carrying the key it
    sat under so a label can be told apart from a data value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _keyed_dates(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _keyed_dates(v, key)
    elif isinstance(obj, str):
        for d in re.findall(r"\d{4}-\d{2}-\d{2}", obj):
            yield key, d


def _find_recent_date(obj: object, today: str) -> tuple[str | None, list[str]]:
    """Freshness stamp for a payload: the most recent date that is not in the
    future, plus any unexpected future-dated values. Declared forward-looking
    labels stay out of both, so a warning that survives is a real defect."""
    dates = _payload_dates(obj)
    past = [d for d in dates if d <= today]
    future = sorted(
        {d for k, d in _keyed_dates(obj) if d > today and k not in _FUTURE_LABEL_KEYS}
    )
    return (max(past) if past else None), future


def qa(base: str, service: str = SERVICE) -> dict:
    base = _qa_destination(base, service)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    findings: list[str] = []
    checks: list[dict] = []

    # 1. Open health probe.
    code, ms, health = _get(f"{base}/api/health")
    up = code == 200 and isinstance(health, dict) and health.get("ok") is True
    checks.append(
        {
            "name": "health",
            "code": code,
            "ms": round(ms),
            "pass": up,
            "detail": health if isinstance(health, dict) else str(health),
        }
    )
    if not up:
        findings.append(f"service health not ok (HTTP {code})")
        return {
            "verdict": "DOWN",
            "today": today,
            "checks": checks,
            "findings": findings,
            "freshest": None,
        }

    gate_on = bool(isinstance(health, dict) and health.get("passcode"))
    if not gate_on:
        findings.append(
            "passcode gate is OFF (UI_PASSCODE unset) - the tool is open to anyone with the URL"
        )

    # 2. Gated data endpoints.
    passcode = _passcode(service) if gate_on else ""
    freshest = None
    metrics_body: dict | None = None
    desk_body: dict | None = None
    for label, path, timeout_s in GATED:
        if gate_on and not passcode:
            checks.append(
                {
                    "name": label,
                    "code": None,
                    "ms": 0,
                    "pass": None,
                    "detail": "SKIPPED (no passcode)",
                }
            )
            continue
        code, ms, body = _get(f"{base}{path}", passcode, timeout_s)
        empty = body in (None, "", {}, []) or (isinstance(body, dict) and not body)
        ok = code == 200 and not empty
        d, future = _find_recent_date(body, today) if ok else (None, [])
        if d and (freshest is None or d > freshest):
            freshest = d
        notes = []
        if future:
            notes.append(f"future dates {','.join(future)}")
            findings.append(
                f"WARN {label}: future-dated value(s) {', '.join(future)} in payload "
                f"(excluded from the freshness stamp)"
            )
        if not ok:
            notes.append(f"HTTP {code}{' empty' if empty else ''}")
            findings.append(f"{label}: HTTP {code}{' / empty body' if empty else ''}")
        if ok and ms > SLOW_MS:
            notes.append(f"slow {round(ms)}ms")
            findings.append(f"{label} slow: {round(ms)}ms (> {SLOW_MS}ms)")
        if label == "metrics" and ok and isinstance(body, dict):
            metrics_body = body
            plat = int(body.get("platforms") or 0)
            notes.append(f"platforms={plat}")
        if label == "desk" and ok and isinstance(body, dict):
            desk_body = body
            heat = body.get("platform_heat") or []
            names = {str(r.get("platform") or "") for r in heat if isinstance(r, dict)}
            missing_gap = KNOWN_GAP_PLATFORMS - names
            if missing_gap:
                notes.append(f"known gap: no {', '.join(sorted(missing_gap))}")
            regained = KNOWN_GAP_PLATFORMS & names
            if regained:
                findings.append(
                    f"desk platform_heat has {', '.join(sorted(regained))} again - "
                    "a known-gap platform is back, drop it from KNOWN_GAP_PLATFORMS"
                )
        checks.append(
            {
                "name": label,
                "code": code,
                "ms": round(ms),
                "pass": ok,
                "detail": "; ".join([d or "ok", *notes] if ok else notes),
            }
        )

    if metrics_body and desk_body:
        heat = desk_body.get("platform_heat") or []
        heat_names = {str(r.get("platform") or "") for r in heat if isinstance(r, dict)}
        plat_count = int(metrics_body.get("platforms") or 0)
        if "X" in heat_names and plat_count < 9:
            findings.append(
                f"metrics platforms={plat_count} stale (desk has X; expect >=9 voice platforms)"
            )
            for c in checks:
                if c["name"] == "metrics":
                    c["pass"] = False
                    c["detail"] = f"platforms={plat_count}; stale vs desk X"

    # 3. Freshness vs today (the tool reads the engine's daily data).
    stale = None
    if freshest:
        lag = (
            datetime.date.fromisoformat(today) - datetime.date.fromisoformat(freshest)
        ).days
        stale = lag > 1
        if stale:
            findings.append(
                f"data looks stale: freshest date {freshest} is {lag} days behind {today}"
            )

    hard = [c for c in checks if c["pass"] is False]
    if not up:
        verdict = "DOWN"
    elif hard or stale:
        verdict = "DEGRADED"
    else:
        verdict = "HEALTHY"
    return {
        "verdict": verdict,
        "today": today,
        "freshest": freshest,
        "stale": stale,
        "checks": checks,
        "findings": findings,
    }


def _append_log(result: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_PATH.exists():
        LOG_PATH.write_text(
            "# 42 QA log\n\nOne line per run from `scripts/lp_qa.py`. "
            "Watch for verdict drops, new findings, latency creep, and freshness lag. "
            "A recurring finding is a continuous-improvement candidate.\n\n"
            "| date | verdict | freshest | slowest | findings |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    slowest = max(
        (c["ms"] for c in result["checks"] if isinstance(c.get("ms"), int)), default=0
    )
    fnd = "; ".join(result["findings"]) if result["findings"] else "none"
    line = f"| {result['today']} | {result['verdict']} | {result['freshest'] or 'n/a'} | {slowest}ms | {fnd} |\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument(
        "--service", default=SERVICE, help="Cloud Run service name for passcode lookup"
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    result = qa(args.base, service=args.service)
    if not args.no_log:
        _append_log(result)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"42 QA  {result['today']}  ->  {result['verdict']}")
        print(
            f"  freshest data: {result['freshest'] or 'unknown'}"
            + ("  (STALE)" if result.get("stale") else "")
        )
        for c in result["checks"]:
            mark = "PASS" if c["pass"] else ("skip" if c["pass"] is None else "FAIL")
            print(f"    [{mark}] {c['name']}: {c['detail']}  ({c['ms']}ms)")
        if result["findings"]:
            print("  FINDINGS (continuous-improvement candidates):")
            for f in result["findings"]:
                print(f"    - {f}")
    return 0 if result["verdict"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
