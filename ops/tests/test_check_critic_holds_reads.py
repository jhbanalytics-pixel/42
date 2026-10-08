"""check_critic_holds: failed reads, the date it needs and the run status it shows, on fake warehouse rows."""
import datetime as dt
import json
import runpy
import sys
from pathlib import Path

import pytest
from google.cloud import bigquery


class Row(dict):
    __getattr__ = dict.get


def critic_line(out):
    [line] = [line for line in out.splitlines() if line.startswith("  critic: ")]
    return line


def run_report(monkeypatch, capsys, checks, rid="brief-fixture", argv_date="2026-10-05", fail=()):
    """Run the script on a fake client; the claim_checks rows are given as (reason, ...) tuples per item."""
    payload = {"status": "partial", "held_back": {"items": [
        {"item_id": "item-fixture", "title": "Topic fixture", "evidence": []}]}}
    answers = {"runs": [Row(run_id=rid, published_at=dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc),
                            run_status="ok", markets="NG partial")],
               "payload": [Row(market="NG", p=json.dumps(payload))], "checks": checks,
               "lanes": [], "enr": [], "ven": []}
    order = ["runs", "payload", "checks", "lanes", "enr", "ven"]
    asked = []

    class Client:
        def query(self, sql, job_config=None):
            name = order[len(asked)]
            asked.append(name)

            class Result:
                def result(self):
                    if name in fail:
                        raise RuntimeError("boom")
                    return answers[name]
            return Result()

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: Client())
    monkeypatch.setattr(sys, "argv", ["check_critic_holds.py", argv_date, rid])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "runners" / "check_critic_holds.py"))
    return capsys.readouterr().out


def test_a_failed_claim_checks_read_is_not_reported_as_a_critic_that_was_not_reached(monkeypatch, capsys):
    out = run_report(monkeypatch, capsys, [], fail=("checks",))
    line = critic_line(out)
    assert "not reached" not in out
    assert "?" in line and "claim_checks read failed" in line


@pytest.mark.parametrize("failing", ["runs", "payload"])
def test_a_failed_runs_or_payload_read_stops_the_report_instead_of_printing_zero_holds(monkeypatch, capsys, failing):
    with pytest.raises(SystemExit) as stopped:
        run_report(monkeypatch, capsys, [], fail=(failing,))
    assert "read failed" in str(stopped.value.code)
    out = capsys.readouterr().out
    assert "0 cards, 0 held" not in out and "critic: not reached" not in out


def test_the_report_needs_its_date_and_never_falls_back_to_a_past_one(monkeypatch, capsys):
    asked = []

    class Client:
        def query(self, sql, job_config=None):
            asked.append(sql)
            raise AssertionError("no read may start without a date")

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: Client())
    monkeypatch.setattr(sys, "argv", ["check_critic_holds.py"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "runners" / "check_critic_holds.py"))
    assert stopped.value.code not in (0, None)
    assert asked == []
    assert "usage" in str(stopped.value.code).lower() or "usage" in capsys.readouterr().err.lower()


RUNS_PROJECT_AGENT = "`ogilvy-trends-v2.intelligence_42_agent`"


class DuckAgentClient:
    """Runs the script's own SQL text on DuckDB over agent.briefs and agent.runs fixtures."""

    def __init__(self, con):
        self.con, self.sql = con, []

    def query(self, sql, job_config=None):
        from core.detect.tests import duck
        self.sql.append(sql)
        params = {p.name: duck._value(p) for p in (job_config.query_parameters if job_config else [])}
        return duck._Job([Row(r) for r in duck.query(self.con, sql.replace(RUNS_PROJECT_AGENT, "agent"), params)])


def brief_runs_world(order):
    from core.detect.tests import duck
    day = dt.date(2026, 10, 5)
    t0 = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.timezone.utc)
    briefs = [
        {"brief_date": day, "market": "NG", "run_id": "brief-a", "published_at": t0, "status": "published",
         "payload": json.dumps({})},
        {"brief_date": day, "market": "ZA", "run_id": "brief-b", "published_at": t0, "status": "published",
         "payload": json.dumps({})},
    ]
    runs = [
        {"run_id": "brief-a", "stage": "brief", "run_date": day, "status": "running", "started_at": t0},
        {"run_id": "brief-a", "stage": "brief", "run_date": day, "status": "ok", "started_at": t0,
         "finished_at": t0 + dt.timedelta(minutes=5)},
        {"run_id": "brief-b", "stage": "brief", "run_date": day, "status": "running", "started_at": t0},
        {"run_id": "brief-b", "stage": "brief", "run_date": day, "status": "failed", "started_at": t0,
         "finished_at": t0 + dt.timedelta(minutes=2)},
    ]
    con = duck.connect(views=False)
    duck.load(con, "agent.briefs", briefs)
    duck.load(con, "agent.runs", runs[::-1] if order == "reversed" else runs)
    return con


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_the_run_status_is_the_terminal_state_whatever_order_the_rows_arrive_in(monkeypatch, capsys, order):
    client = DuckAgentClient(brief_runs_world(order))
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: client)
    monkeypatch.setattr(sys, "argv", ["check_critic_holds.py", "2026-10-05"])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "runners" / "check_critic_holds.py"))
    out = capsys.readouterr().out
    assert "brief-a" in out and "run ok" in out and "run failed" in out
    assert "run running" not in out
    chosen = [line for line in out.splitlines() if line.startswith("== reading run")]
    assert chosen == ["== reading run brief-b"]  # published at the same instant: the higher run id, not a coin toss
