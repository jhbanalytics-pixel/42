"""Staging smoke test for f42-api (task 1.14): health, today, one T1 question end to end.

    py -3.13 core/api/smoke.py <base_url> [--question "..."] [--market ZA] [--mode live|replay] [--ask-timeout 900]
        [--today-max-age-hours 36]

The passcode comes only from the environment variable F42_SMOKE_PASSCODE. It is never
printed, and no line this script prints carries a header value or an exception's text.
Each check prints "PASS <name>: <evidence>" or "FAIL <name>: <reason>". Exit 0 only when
every check passed, 1 when any failed, 2 when the passcode is not set. While the ask runs,
a progress line goes to stderr every PROGRESS_SECONDS with its ask_id and step count.

The today check always says on stderr how old the Today brief it read is: its date, its published time in SAST and
its age in hours, or that the age cannot be read. It fails on age only when a maximum is given, by
--today-max-age-hours or the environment variable F42_SMOKE_TODAY_MAX_AGE_HOURS (the flag wins): a brief older than
that many hours, or one whose age cannot be read, is then a FAIL. With neither set, age never fails the check. A
maximum that is not a number of hours above 0 exits 2 before any check runs.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

import httpx

DEFAULT_QUESTION = "What are people in South Africa posting about most this week?"
TIMEOUT = 30.0
# The ask check starts the ask (202) and reads its record, every POLL_SECONDS once past the first few reads, the way
# the app falls back when its stream drops, until it finishes or ASK_TIMEOUT seconds have passed in all. A live T1
# ask can take many minutes.
START_TIMEOUT = 120.0
ASK_TIMEOUT = 900.0
POLL_SECONDS = 5.0
PROGRESS_SECONDS = 30.0
MARKETS = ("ZA", "NG", "KE")
MAX_AGE_ENV = "F42_SMOKE_TODAY_MAX_AGE_HOURS"
SAST = dt.timezone(dt.timedelta(hours=2), "SAST")
_now = lambda: dt.datetime.now(dt.timezone.utc)  # noqa: E731 - replaced in tests
_clock = time.monotonic
_sleep = time.sleep


def _why(resp: httpx.Response) -> str:
    """HTTP status plus the contract's error code and message, never a header value."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    if isinstance(body, dict) and "error" in body:
        return f"HTTP {resp.status_code} {body.get('error')}: {body.get('message')}"
    return f"HTTP {resp.status_code}"


def _failed(exc: Exception) -> str:
    # Only the type name: an exception's text can carry the request and its headers.
    return f"request failed ({type(exc).__name__})"


def check_health(client, base):
    resp = client.get(f"{base}/api/health")
    if resp.status_code != 200:
        return False, _why(resp)
    body = resp.json()
    checks = body.get("checks") or {}
    shown = ", ".join(f"{k}={v}" for k, v in checks.items()) or "no checks"
    if body.get("ok") is not True:
        return False, f"ok is {json.dumps(body.get('ok'))}; {shown}"
    return True, f"ok true; {shown}"


def check_gate(client, base):
    resp = client.get(f"{base}/api/today")
    if resp.status_code != 401:
        return False, f"expected 401 without a passcode, got {_why(resp)}"
    return True, "401 without a passcode"


def _today_age(body, max_age_hours):
    """Say on stderr how old the Today brief is; return a FAIL reason when a set maximum is passed, else None."""
    date = body.get("date")
    try:
        published = dt.datetime.fromisoformat(body.get("published_at"))
        hours = (_now() - published).total_seconds() / 3600
    except (TypeError, ValueError):
        # No published_at, an unparseable one, or one without a zone cannot be aged.
        _progress(f"Today brief for {date}: its age cannot be read (no usable published_at)")
        if max_age_hours is None:
            return None
        return f"the age of the Today brief for {date} cannot be read, and a {max_age_hours:g} hour maximum is set"
    limit = "no max age set" if max_age_hours is None else f"max age {max_age_hours:g} hours"
    _progress(f"Today brief for {date} was published {published.astimezone(SAST):%Y-%m-%d %H:%M} SAST, "
              f"{hours:.1f} hours ago ({limit})")
    if max_age_hours is not None and hours > max_age_hours:
        return (f"the Today brief for {date} is {hours:.1f} hours old, "
                f"older than the {max_age_hours:g} hour maximum")
    return None


def check_today(client, base, headers, max_age_hours=None):
    resp = client.get(f"{base}/api/today", headers=headers)
    if resp.status_code != 200:
        return False, _why(resp)
    body = resp.json()
    markets = body.get("markets") or []
    codes = [m.get("market") for m in markets]
    if sorted(codes) != sorted(MARKETS):
        return False, f"expected markets ZA, NG, KE, got {', '.join(map(str, codes)) or 'none'}"
    stale = _today_age(body, max_age_hours)
    if stale:
        return False, stale
    cards = ", ".join(f"{m['market']} {len(m.get('cards') or [])} ({m.get('status')})" for m in markets)
    return True, f"date {body.get('date')}, status {body.get('status')}, cards {cards}"


def check_ask_record(record):
    """The Ask record from a wait=true ask: finished, answered, every claim cited."""
    status = record.get("status")
    if status not in ("complete", "stopped"):
        err = record.get("error") or {}
        return False, f"ask status {status}: {err.get('message') or 'no answer'}"
    answer = record.get("answer")
    if not isinstance(answer, dict):
        return False, f"ask status {status} but the answer is null"
    evidence = answer.get("evidence") or []
    ids = {e.get("id") for e in evidence}
    claims = answer.get("claims") or []
    for claim in claims:
        cited = claim.get("evidence_ids") or []
        if not cited:
            return False, f"claim {claim.get('id')} cites no evidence"
        missing = [i for i in cited if i not in ids]
        if missing:
            return False, f"claim {claim.get('id')} cites evidence not in evidence[]: {', '.join(map(str, missing))}"
    platforms = sorted({str(e.get("platform")) for e in evidence if e.get("platform")})
    run = record.get("run") or {}
    return True, (
        f"ask_id {record.get('ask_id')}, answer {answer.get('status')}, {len(claims)} claims, "
        f"{len(evidence)} evidence, platforms {', '.join(platforms) or 'none'}, "
        f"credits {run.get('credits')}, {run.get('seconds')} s"
    )


def _progress(text):
    # Progress goes to stderr, so stdout keeps one PASS or FAIL line per check.
    print(f"... {text}", file=sys.stderr, flush=True)


def check_ask(client, base, headers, question, market, mode, deadline=None):
    """Start one ask and follow its record to a final status, with deadline (default ASK_TIMEOUT) seconds for the
    whole wait.

    Returns (ok, evidence, ask_id). An ask still running at the deadline is a FAIL that names its id, and no id is
    passed on, so events and export are skipped rather than following a stream that is still open."""
    deadline = ASK_TIMEOUT if deadline is None else deadline
    body = {"question": question, "market": market, "tier": "T1", "mode": mode, "wait": False}
    started = _clock()
    resp = client.post(f"{base}/api/ask", json=body, headers=headers, timeout=START_TIMEOUT)
    if resp.status_code not in (200, 202):
        return False, _why(resp), None
    record = resp.json()
    ask_id = record.get("ask_id")
    if not isinstance(ask_id, str) or not ask_id:
        return False, f"HTTP {resp.status_code} without an ask_id", None
    _progress(f"ask {ask_id} started; following it for up to {deadline:g} s")
    shown, polls = started, 0
    while record.get("status") == "running":
        elapsed = _clock() - started
        if elapsed >= deadline:
            steps = len(record.get("steps") or [])
            return False, (f"ask {ask_id} still running after {elapsed:.0f} s ({steps} steps so far); "
                           f"it was not stopped, read it later at /api/ask/{ask_id}"), None
        if _clock() - shown >= PROGRESS_SECONDS:
            shown = _clock()
            _progress(f"ask {ask_id} running, {len(record.get('steps') or [])} steps, {elapsed:.0f} s")
        if polls:
            # A quick first few reads for a short ask, then every POLL_SECONDS, never past the deadline.
            _sleep(min(POLL_SECONDS, 0.25 * 2 ** (polls - 1), max(0.0, deadline - elapsed)))
        polls += 1
        resp = client.get(f"{base}/api/ask/{ask_id}", headers=headers)
        if resp.status_code != 200:
            return False, f"ask {ask_id}: {_why(resp)}", None
        record = resp.json()
    _progress(f"ask {ask_id} {record.get('status')} after {_clock() - started:.0f} s")
    ok, evidence = check_ask_record(record)
    return ok, evidence, ask_id


def _sse_events(text):
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name = None
        data = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name:
            events.append((name, "\n".join(data)))
    return events


def check_events(client, base, headers, ask_id):
    resp = client.get(f"{base}/api/ask/{ask_id}/events", headers=headers)
    if resp.status_code != 200:
        return False, _why(resp)
    events = _sse_events(resp.text)
    steps = sum(1 for name, _ in events if name == "step")
    if not events or events[-1][0] != "done":
        return False, f"stream ended without a done event after {len(events)} events"
    try:
        done = json.loads(events[-1][1]).get("status")
    except ValueError:
        done = "unreadable"
    return True, f"{steps} steps replayed, {len(events)} events, done {done}"


def check_export(client, base, headers, ask_id):
    resp = client.get(f"{base}/api/ask/{ask_id}/export", params={"format": "html"}, headers=headers)
    if resp.status_code != 200:
        return False, _why(resp)
    if not resp.headers.get("content-type", "").startswith("text/html"):
        return False, "the export is not text/html"
    return True, f"text/html, {len(resp.content)} bytes"


def run(base_url, passcode, question, market, mode, client=None, ask_timeout=None, today_max_age_hours=None):
    """Run every check in order, print one line each, return [(name, passed, evidence)]."""
    base = base_url.rstrip("/")
    headers = {"X-Passcode": passcode}
    own = client is None
    if own:
        client = httpx.Client(timeout=TIMEOUT)
    results = []
    state = {"ask_id": None}

    def record(name, fn):
        try:
            out = fn()
        except Exception as exc:
            out = (False, _failed(exc))
        ok, evidence = out[0], out[1]
        if len(out) > 2:
            state["ask_id"] = out[2]
        results.append((name, ok, evidence))
        print(f"{'PASS' if ok else 'FAIL'} {name}: {evidence}", flush=True)

    def needs_ask(fn):
        def go():
            if not state["ask_id"]:
                return False, "skipped: the ask check returned no ask_id"
            return fn(client, base, headers, state["ask_id"])
        return go

    try:
        record("health", lambda: check_health(client, base))
        record("gate", lambda: check_gate(client, base))
        record("today", lambda: check_today(client, base, headers, today_max_age_hours))
        record("ask", lambda: check_ask(client, base, headers, question, market, mode, ask_timeout))
        record("events", needs_ask(check_events))
        record("export", needs_ask(check_export))
    finally:
        if own:
            client.close()
    return results


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description="Smoke test a deployed f42-api.")
    parser.add_argument("base_url")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--market", default="ZA", choices=MARKETS)
    parser.add_argument("--mode", default="live", choices=("live", "replay"))
    parser.add_argument("--ask-timeout", type=float, default=None,
                        help=f"seconds to follow the ask before failing it (default {ASK_TIMEOUT:g})")
    parser.add_argument("--today-max-age-hours", type=float, default=None,
                        help=f"fail the today check when the brief is older than this many hours "
                             f"(default from {MAX_AGE_ENV}; unset means age never fails it)")
    args = parser.parse_args(argv)
    max_age = args.today_max_age_hours
    if max_age is None and os.environ.get(MAX_AGE_ENV, "").strip():
        try:
            max_age = float(os.environ[MAX_AGE_ENV])
        except ValueError:
            parser.error(f"{MAX_AGE_ENV} must be a number of hours")
    if max_age is not None and not max_age > 0:
        parser.error(f"the Today max age (--today-max-age-hours or {MAX_AGE_ENV}) must be more than 0 hours")
    passcode = os.environ.get("F42_SMOKE_PASSCODE", "")
    if not passcode:
        print("F42_SMOKE_PASSCODE is not set. Set it to the app passcode and run again.", file=sys.stderr)
        return 2
    results = run(args.base_url, passcode, args.question, args.market, args.mode, client=client,
                  ask_timeout=args.ask_timeout, today_max_age_hours=max_age)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed} of {len(results)} checks passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
