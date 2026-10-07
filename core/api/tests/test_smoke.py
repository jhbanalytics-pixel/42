"""The staging smoke test (task 1.14) run against the real f42-api in process, on fixtures."""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from core.api import app as api_mod
from core.api import auth
from core.api import smoke

PASS = "smoke-pass-7Qx9w"
WRONG = "wrong-pass-Zk3m"
BASE = "http://testserver"
NAMES = ["health", "gate", "today", "ask", "events", "export"]
SMOKE = Path(smoke.__file__)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0")
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.delenv("F42_SMOKE_PASSCODE", raising=False)
    monkeypatch.delenv("F42_SMOKE_TODAY_MAX_AGE_HOURS", raising=False)
    # Every full run makes passcode attempts; start each test with a clear limiter so the gate never answers 429.
    auth.auth_limiter.hits.clear()


@pytest.fixture
def client():
    with TestClient(api_mod.app, base_url=BASE) as c:
        yield c


def by_name(results):
    return {name: (ok, evidence) for name, ok, evidence in results}


def test_all_checks_pass_against_fixtures(client, capsys):
    results = smoke.run(BASE, PASS, smoke.DEFAULT_QUESTION, "ZA", "live", client=client)
    assert [r[0] for r in results] == NAMES
    failed = [r for r in results if not r[1]]
    assert failed == []
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert [ln.split(":")[0] for ln in lines] == [f"PASS {n}" for n in NAMES]
    got = by_name(results)
    assert "bigquery" in got["health"][1] and "agent" in got["health"][1]
    assert "2026-09-30" in got["today"][1]
    assert "ZA" in got["today"][1] and "NG" in got["today"][1] and "KE" in got["today"][1]
    assert "a_" in got["ask"][1] and "complete" in got["ask"][1]
    assert "tiktok" in got["ask"][1] and "credits 214" in got["ask"][1]
    assert "step" in got["events"][1]
    assert PASS not in out.out and PASS not in out.err


def test_replay_mode_and_other_market_pass(client, capsys):
    results = smoke.run(BASE, PASS, "What is new in Nigeria this week?", "NG", "replay", client=client)
    assert all(ok for _, ok, _ in results), results
    out = capsys.readouterr()
    assert PASS not in out.out and PASS not in out.err


def test_wrong_passcode_fails_today_and_ask(client, capsys):
    results = smoke.run(BASE, WRONG, smoke.DEFAULT_QUESTION, "ZA", "live", client=client)
    got = by_name(results)
    assert got["health"][0] is True
    assert got["gate"][0] is True
    assert got["today"][0] is False and "401" in got["today"][1]
    assert got["ask"][0] is False and "401" in got["ask"][1]
    assert got["events"][0] is False
    assert got["export"][0] is False
    out = capsys.readouterr()
    for secret in (PASS, WRONG):
        assert secret not in out.out and secret not in out.err


def test_failed_ask_is_a_fail(client, capsys):
    results = smoke.run(BASE, PASS, "Make this one fail please", "ZA", "live", client=client)
    got = by_name(results)
    assert got["ask"][0] is False and "failed" in got["ask"][1]
    out = capsys.readouterr()
    assert PASS not in out.out and PASS not in out.err


def test_today_not_ready_is_a_fail_with_the_message(client, monkeypatch, capsys):
    from core.api import today

    def not_ready(store, date=None):
        raise today.NotReady("no brief")

    monkeypatch.setattr(today, "build_today", not_ready)
    got = by_name(smoke.run(BASE, PASS, smoke.DEFAULT_QUESTION, "ZA", "live", client=client))
    assert got["today"][0] is False
    assert "409" in got["today"][1] and "not published" in got["today"][1]
    out = capsys.readouterr()
    assert PASS not in out.out and PASS not in out.err


def test_uncited_claim_fails_the_ask_check():
    record = {
        "ask_id": "a_1", "status": "complete",
        "answer": {"status": "complete", "short_answer": "The cited posts support this claim.",
                   "claims": [{"id": "c1", "evidence_ids": ["tt_1", "missing"]}],
                   "evidence": [{"id": "tt_1", "platform": "tiktok"}]},
        "run": {"credits": 1, "seconds": 2},
    }
    ok, why = smoke.check_ask_record(record)
    assert ok is False and "missing" in why
    record["answer"]["claims"] = [{"id": "c1", "evidence_ids": []}]
    ok, why = smoke.check_ask_record(record)
    assert ok is False and "c1" in why
    record["answer"]["claims"] = [{"id": "c1", "evidence_ids": ["tt_1"]}]
    ok, _ = smoke.check_ask_record(record)
    assert ok is True


@pytest.mark.parametrize("short_answer, status, gap_kind, expected", [
    ("", "complete", "exact", False),
    (" \t\n", "complete", "exact", False),
    ("", "partial", "absent", False),
    ("", "partial", "similar", False),
    ("", "partial", "incomplete", False),
    ("", "partial", "different_reason", False),
    ("", "partial", "exact", True),
    (" \t\n", "partial", "exact", True),
    ("The cited posts support this summary.", "complete", "absent", True),
    ("The cited posts support this summary.", "partial", "absent", True),
])
def test_ask_summary_requires_text_or_the_exact_partial_headline_gap(short_answer, status, gap_kind, expected):
    import json

    from core.agent.writer import HEADLINE_GAP

    record = json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))
    answer = record["answer"]
    answer.update(short_answer=short_answer, status=status, gaps=[])
    if gap_kind != "absent":
        gap = dict(HEADLINE_GAP)
        if gap_kind == "similar":
            gap["what"] += " but unrelated"
        elif gap_kind == "incomplete":
            gap.pop("searched")
        elif gap_kind == "different_reason":
            gap["why"] = "another reason"
        answer["gaps"].append(gap)
    ok, reason = smoke.check_ask_record(record)
    assert ok is expected, reason
    if not expected:
        assert "summary" in reason


def test_standalone_summary_check_reads_the_producer_gap_outside_the_repository(tmp_path):
    import json

    from core.agent.writer import HEADLINE_GAP

    record = json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))
    record["answer"].update(short_answer="", status="complete", gaps=[dict(HEADLINE_GAP)])
    code = ("import json, runpy, sys; module = runpy.run_path(sys.argv[1]); "
            "record = json.loads(sys.argv[2]); "
            "assert module['check_ask_record'](record)[0] is False; "
            "record['answer']['status'] = 'partial'; "
            "assert module['check_ask_record'](record)[0] is True")
    proc = subprocess.run([sys.executable, "-c", code, str(SMOKE), json.dumps(record)],
                          cwd=tmp_path, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode("utf-8")


def test_transport_error_never_prints_header_values(capsys):
    def boom(request):
        raise httpx.ConnectError(f"cannot connect, header was {request.headers.get('x-passcode')}")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c:
        results = smoke.run(BASE, PASS, smoke.DEFAULT_QUESTION, "ZA", "live", client=c)
    assert [r[0] for r in results] == NAMES
    assert not any(ok for _, ok, _ in results)
    out = capsys.readouterr()
    assert "ConnectError" in out.out
    assert PASS not in out.out and PASS not in out.err
    assert all(PASS not in evidence for _, _, evidence in results)


def test_main_exit_codes(client, monkeypatch, capsys):
    monkeypatch.setenv("F42_SMOKE_PASSCODE", PASS)
    assert smoke.main([BASE], client=client) == 0
    monkeypatch.setenv("F42_SMOKE_PASSCODE", WRONG)
    assert smoke.main([BASE, "--mode", "replay", "--market", "KE"], client=client) == 1
    out = capsys.readouterr()
    for secret in (PASS, WRONG):
        assert secret not in out.out and secret not in out.err


def test_main_missing_passcode_exits_2(monkeypatch, capsys):
    assert smoke.main([BASE]) == 2
    out = capsys.readouterr()
    assert "F42_SMOKE_PASSCODE" in out.err


def test_script_missing_passcode_exits_2_without_network():
    env = {k: v for k, v in os.environ.items() if k != "F42_SMOKE_PASSCODE"}
    proc = subprocess.run(
        [sys.executable, str(SMOKE), "http://127.0.0.1:9"],
        capture_output=True, env=env, timeout=60,
    )
    assert proc.returncode == 2
    assert b"F42_SMOKE_PASSCODE" in proc.stderr


FIXTURE_ASK = Path(smoke.__file__).resolve().parent / "fixtures" / "ask_complete.json"


def ask_server(statuses, posted):
    """A fake f42-api for the ask check: POST /api/ask answers 202, each GET of the record takes the next status."""
    import json

    record = json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))
    record["ask_id"] = "a_20261002_slow0001"
    left = list(statuses)

    def handle(request):
        if request.method == "POST" and request.url.path == "/api/ask":
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"ask_id": record["ask_id"], "status": "running",
                                             "events_url": f"/api/ask/{record['ask_id']}/events",
                                             "url": f"/api/ask/{record['ask_id']}"})
        if request.method == "GET" and request.url.path == f"/api/ask/{record['ask_id']}":
            status = left.pop(0) if len(left) > 1 else left[0]
            body = dict(record, status=status)
            if status == "running":
                body.update(answer=None, run=None, finished_at=None)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not_found", "message": "no route"})

    return handle


def test_an_ask_that_never_finishes_fails_at_the_deadline_with_its_id(monkeypatch, capsys):
    clock = {"now": 0.0}
    monkeypatch.setattr(smoke, "ASK_TIMEOUT", 60.0)
    monkeypatch.setattr(smoke, "POLL_SECONDS", 5.0, raising=False)
    monkeypatch.setattr(smoke, "_clock", lambda: clock["now"], raising=False)
    monkeypatch.setattr(smoke, "_sleep", lambda s: clock.__setitem__("now", clock["now"] + s), raising=False)
    posted = []
    with httpx.Client(transport=httpx.MockTransport(ask_server(["running"], posted))) as c:
        ok, evidence, ask_id = smoke.check_ask(c, BASE, {"X-Passcode": PASS}, smoke.DEFAULT_QUESTION, "ZA", "live")
    assert ok is False
    assert "a_20261002_slow0001" in evidence and "still running" in evidence
    # The ask is followed the way the app follows it: a 202, then the record, never one silent wait=true request.
    assert posted and posted[0].get("wait") is False
    assert 60.0 <= clock["now"] <= 70.0
    # No id goes on, so events and export are skipped instead of following a stream that is still open.
    assert ask_id is None
    err = capsys.readouterr().err
    assert "a_20261002_slow0001" in err and "running" in err
    assert PASS not in err


def test_a_slow_ask_shows_progress_and_passes_when_it_completes(monkeypatch, capsys):
    clock = {"now": 0.0}
    monkeypatch.setattr(smoke, "ASK_TIMEOUT", 600.0)
    monkeypatch.setattr(smoke, "POLL_SECONDS", 5.0, raising=False)
    monkeypatch.setattr(smoke, "_clock", lambda: clock["now"], raising=False)
    monkeypatch.setattr(smoke, "_sleep", lambda s: clock.__setitem__("now", clock["now"] + s), raising=False)
    posted = []
    statuses = ["running"] * 20 + ["complete"]
    with httpx.Client(transport=httpx.MockTransport(ask_server(statuses, posted))) as c:
        ok, evidence, ask_id = smoke.check_ask(c, BASE, {"X-Passcode": PASS}, smoke.DEFAULT_QUESTION, "ZA", "live")
    assert ok is True, evidence
    assert ask_id == "a_20261002_slow0001"
    out = capsys.readouterr()
    assert out.out == ""
    progress = [ln for ln in out.err.splitlines() if "a_20261002_slow0001" in ln]
    assert len(progress) >= 2 and any("6 steps" in ln for ln in progress)
    assert PASS not in out.err


PUBLISHED = "2026-09-30T06:14:40+02:00"


def freeze(monkeypatch, iso):
    import datetime as dt

    monkeypatch.setattr(smoke, "_now", lambda: dt.datetime.fromisoformat(iso), raising=False)


def today_server(published_at):
    """A fake f42-api that answers /api/today with three markets and the given published_at."""
    def handle(request):
        if request.url.path == "/api/today":
            markets = [{"market": m, "status": "published", "cards": []} for m in smoke.MARKETS]
            return httpx.Response(200, json={"date": "2026-09-30", "status": "published",
                                             "published_at": published_at, "markets": markets})
        return httpx.Response(404, json={"error": "not_found", "message": "no route"})

    return handle


def test_today_age_is_printed_and_an_old_brief_still_passes_without_a_max_age(client, monkeypatch, capsys):
    freeze(monkeypatch, "2026-10-05T08:14:40+02:00")
    results = smoke.run(BASE, PASS, smoke.DEFAULT_QUESTION, "ZA", "live", client=client)
    assert all(ok for _, ok, _ in results), results
    out = capsys.readouterr()
    assert [ln.split(":")[0] for ln in out.out.strip().splitlines()] == [f"PASS {n}" for n in NAMES]
    age = [ln for ln in out.err.splitlines() if "Today brief" in ln]
    assert age == ["... Today brief for 2026-09-30 was published 2026-09-30 06:14 SAST, 122.0 hours ago "
                   "(no max age set)"]


def test_today_older_than_the_max_age_fails_and_the_smoke_exits_1(client, monkeypatch, capsys):
    monkeypatch.setenv("F42_SMOKE_PASSCODE", PASS)
    monkeypatch.setenv("F42_SMOKE_TODAY_MAX_AGE_HOURS", "36")
    freeze(monkeypatch, "2026-10-05T08:14:40+02:00")
    assert smoke.main([BASE], client=client) == 1
    out = capsys.readouterr()
    assert ("FAIL today: the Today brief for 2026-09-30 is 122.0 hours old, older than the 36 hour maximum"
            in out.out.splitlines())
    assert "122.0 hours ago (max age 36 hours)" in out.err
    assert PASS not in out.out and PASS not in out.err


def test_today_max_age_boundary_and_flag(monkeypatch, capsys):
    headers = {"X-Passcode": PASS}
    with httpx.Client(transport=httpx.MockTransport(today_server(PUBLISHED))) as c:
        freeze(monkeypatch, "2026-10-01T06:14:40+02:00")
        ok, evidence = smoke.check_today(c, BASE, headers, max_age_hours=24)
        assert ok is True, evidence
        freeze(monkeypatch, "2026-10-01T06:14:41+02:00")
        ok, evidence = smoke.check_today(c, BASE, headers, max_age_hours=24)
        assert ok is False and "older than the 24 hour maximum" in evidence
    monkeypatch.setenv("F42_SMOKE_PASSCODE", PASS)
    monkeypatch.setenv("F42_SMOKE_TODAY_MAX_AGE_HOURS", "1")
    seen = []
    monkeypatch.setattr(smoke, "run", lambda *a, **k: seen.append(k.get("today_max_age_hours")) or [])
    smoke.main([BASE, "--today-max-age-hours", "48"])
    smoke.main([BASE])
    monkeypatch.delenv("F42_SMOKE_TODAY_MAX_AGE_HOURS")
    smoke.main([BASE])
    assert seen == [48.0, 1.0, None]


def test_today_bad_max_age_setting_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("F42_SMOKE_PASSCODE", PASS)
    for bad in ("soon", "0", "-3"):
        monkeypatch.setenv("F42_SMOKE_TODAY_MAX_AGE_HOURS", bad)
        with pytest.raises(SystemExit) as exc:
            smoke.main([BASE])
        assert exc.value.code == 2
        assert "F42_SMOKE_TODAY_MAX_AGE_HOURS" in capsys.readouterr().err


@pytest.mark.parametrize("published_at", [None, "not a time", "2026-09-30T06:14:40"])
def test_today_unreadable_age_is_said_plainly_and_fails_only_with_a_max_age(monkeypatch, capsys, published_at):
    freeze(monkeypatch, "2026-10-01T06:14:40+02:00")
    headers = {"X-Passcode": PASS}
    with httpx.Client(transport=httpx.MockTransport(today_server(published_at))) as c:
        ok, evidence = smoke.check_today(c, BASE, headers)
        assert ok is True, evidence
        assert "... Today brief for 2026-09-30: its age cannot be read (no usable published_at)" in capsys.readouterr().err
        ok, evidence = smoke.check_today(c, BASE, headers, max_age_hours=24)
        assert ok is False
        assert evidence == "the age of the Today brief for 2026-09-30 cannot be read, and a 24 hour maximum is set"
