"""Staging smoke test for f42-api (task 1.14): health, today, one T1 question end to end.

    py -3.13 core/api/smoke.py <base_url> [--question "..."] [--market ZA] [--mode live|replay] [--ask-timeout 900]
        [--today-max-age-hours 36]

The passcode comes only from the environment variable F42_SMOKE_PASSCODE. It is never
printed, and no line this script prints carries a header value or an exception's text.
Each check prints "PASS <name>: <evidence>" or "FAIL <name>: <reason>". Exit 0 only when
every check passed, 1 when any failed, 2 when the passcode is not set. After the "N of M checks passed" line it prints
one last line, "SMOKE-RESULT base=<url> checks=<n> passed=<k>", the script's own statement of what it checked. While the ask runs,
a progress line goes to stderr every PROGRESS_SECONDS with its ask_id and step count.

The today check always says on stderr how old the Today brief it read is: its date, its published time in SAST and
its age in hours, or that the age cannot be read. It fails on age only when a maximum is given, by
--today-max-age-hours or the environment variable F42_SMOKE_TODAY_MAX_AGE_HOURS (the flag wins): a brief older than
that many hours, or one whose age cannot be read, is then a FAIL. With neither set, age never fails the check. A
maximum that is not a number of hours above 0 exits 2 before any check runs. A market with no cards passes the check
with a warning, in its evidence and as a WARN line on stderr: a brief can honestly hold everything back.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
# W8-DEC-07. While False, a blank summary passes only when the producer's verified state is `removed` and the answer
# also carries the exact HEADLINE_GAP and a removal at the support check or the critic: the a80 rule with a verified
# state added, which accepts nothing a80 refuses. When True a blank also passes for any verified removal whose stages
# the producer writes, without the HEADLINE_GAP conjunct. `blank_unexplained` and `unattributed` never pass.
ACCEPT_ANY_VERIFIED_REMOVAL = False
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
    evidence = f"date {body.get('date')}, status {body.get('status')}, cards {cards}"
    empty = [f"{m['market']} ({m.get('status')})" for m in markets if not m.get("cards")]
    if empty:
        # A brief with no cards is a real outcome (everything held, or a data issue), so it warns and does not fail.
        _progress(f"WARN today: no cards for {', '.join(empty)}")
        evidence += f"; warning: no cards for {', '.join(empty)}"
    return True, evidence


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
    problem = _state_problem(record)
    if problem:
        return False, f"ask record carries no verified answer state ({problem})"
    short_answer = answer.get("short_answer")
    blank = not isinstance(short_answer, str) or not short_answer.strip()
    why = _blank_summary_problem(answer, record["answer_meta"]) if blank else _shown_summary_problem(answer, record["answer_meta"])
    if why:
        return False, why
    platforms = sorted({str(e.get("platform")) for e in evidence if e.get("platform")})
    run = record.get("run") or {}
    return True, (
        f"ask_id {record.get('ask_id')}, answer {answer.get('status')}, {len(claims)} claims, "
        f"{len(evidence)} evidence, platforms {', '.join(platforms) or 'none'}, "
        f"credits {run.get('credits')}, {run.get('seconds')} s, summary {record['answer_meta']['summary']['state']}"
    )


BLANK_PROBLEM = "blank summary without a verified producer removal state"
CODE_CAUSES = ("K2", "K3", "K6", "K8", "K9")
# The reason ask.budget_stop_words keys each stop reason of the typed state on.
STOP_REASON_WORDS = {"budget_full": None, "model_call_unverified": "usage_unknown", "price_unreadable": "model_price_invalid"}


def _state_problem(record):
    """The reason the record carries no verified state, or None. The smoke reads the record f42-api returns, so the
    state is a wire value, and answer_state.check_wire is the first line; the checks below are the second."""
    meta = record.get("answer_meta")
    if not isinstance(meta, dict):
        return "no answer_meta"
    if meta.get("check") != "verified":
        return str(meta.get("problem") or meta.get("check") or "no check")
    try:
        import importlib

        check_wire = importlib.import_module("core.agent.answer_state").check_wire
    except ImportError:
        return "answer_state is not available"
    problem = check_wire(record)
    if problem is not None:
        return str(problem)
    try:
        execution, summary = meta["execution"], meta["summary"]
        ok = (isinstance(execution["state"], str) and isinstance(summary["state"], str)
              and all(isinstance(r["stage"], str) and isinstance(r["cause"], str) for r in summary["removals"]))
    except (KeyError, TypeError):
        ok = False
    return None if ok else "shape"


def _expected_gaps(removals):
    """The gap each removal leaves in the stored answer, from the producer's own constants and `plain` (C1 2.5). A
    (stage, cause) pair the producer never writes returns None."""
    from core.agent import checks, plain, writer

    critic = any(stage == "critic" for stage, _ in removals)
    wanted = []
    for stage, cause in removals:
        if stage in ("first_check", "recheck") and cause in CODE_CAUSES or stage == "field_check" and cause in ("K3", "K9"):
            wanted.append(("text", plain.gap(checks._code_gap("Short answer removed", cause, "the short answer text"))))
        elif stage == "field_check" and cause == "K6":
            wanted.append(("text", plain.gap({"what": f"Short answer removed: {writer.DEMOGRAPHIC_WHAT}",
                                              "why": f"field check: {writer.FIELD_WHY}"})))
        elif stage == "field_check" and cause == "field_unchecked":
            wanted.append(("text", plain.gap({"what": f"Short answer removed: {writer.FIELD_UNCHECKED_WHAT}",
                                              "why": writer.FIELD_UNCHECKED_WHY})))
        elif stage == "support_check" and cause == "claim_cut":
            if not critic:
                wanted.append(("exact", dict(writer.HEADLINE_GAP)))
        elif stage == "support_check" and cause == "claim_narrowed":
            if not critic:
                wanted += [("exact", dict(writer.HEADLINE_GAP)), ("exact", dict(writer.NARROWED_HEADLINE_GAP))]
        elif stage == "critic" and cause == "claim_cut":
            wanted.append(("exact", dict(writer.HEADLINE_GAP)))
        else:
            return None
    return wanted


def _has_gap(gaps, kind, gap):
    if kind == "exact":
        return gap in gaps
    return any(isinstance(g, dict) and g.get("what") == gap["what"] and g.get("why") == gap["why"] for g in gaps)


def _blank_summary_problem(answer, meta):
    """A blank summary passes only behind a verified `removed` state (C1 6.5)."""
    summary, execution = meta["summary"], meta["execution"]
    if summary["state"] != "removed" or answer.get("status") != "partial" or execution["state"] != "completed":
        return BLANK_PROBLEM
    removals = [(r["stage"], r["cause"]) for r in summary["removals"]]
    gaps = answer.get("gaps")
    # `unattributed` has no gap the producer writes for it, so _expected_gaps refuses it below, in either setting.
    if not removals or not isinstance(gaps, list):
        return BLANK_PROBLEM
    wanted = _expected_gaps(removals)
    if wanted is None or not all(_has_gap(gaps, kind, gap) for kind, gap in wanted):
        return BLANK_PROBLEM + " (an expected removal gap is missing)"
    if not ACCEPT_ANY_VERIFIED_REMOVAL:
        from core.agent.writer import HEADLINE_GAP

        if HEADLINE_GAP not in gaps or not any(stage in ("support_check", "critic") for stage, _ in removals):
            return BLANK_PROBLEM
    return None


def _fixed_text(execution):
    """The summary the producer writes for an execution state that ends without a written answer."""
    from datetime import date

    from core.agent import ask, checks

    state, reason = execution["state"], execution.get("stop_reason")
    window = (date(2026, 1, 1), date(2026, 1, 7))
    if state == "completed":
        return checks.INSUFFICIENT
    if state == "stopped_on_request":
        return ask._stopped_answer("2026-01-07", [], window)["short_answer"]
    if state == "stopped_on_budget" and reason in STOP_REASON_WORDS:
        return ask.budget_stop_words(STOP_REASON_WORDS[reason])[1]
    if state == "refused_budget_spent":
        return ask._refused_answer("2026-01-07", window)["short_answer"]
    return None


def _shown_summary_problem(answer, meta):
    state = meta["summary"]["state"]
    if state in ("shown", "shown_rewritten"):
        if answer.get("status") in ("complete", "partial"):
            return None
        return f"summary state {state} with answer status {answer.get('status')}"
    if state == "fixed_text":
        fixed = _fixed_text(meta["execution"])
        if fixed is not None and answer.get("short_answer") == fixed and answer.get("status") == "insufficient_evidence":
            return None
        return f"the summary is not the fixed text of its execution state ({meta['execution']['state']})"
    return f"the summary is shown but its state is {state}"


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
    # The release paste reads this line, so the base and the counts are the script's own statement of what it checked.
    print(f"SMOKE-RESULT base={args.base_url.rstrip('/')} checks={len(results)} passed={passed}", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
