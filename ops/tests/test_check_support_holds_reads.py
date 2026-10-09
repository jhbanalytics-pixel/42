"""check_support_holds: failed reads, the date it needs and the run status it shows, on fake warehouse rows."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_support_holds", ROOT / "ops" / "runners" / "check_support_holds.py")
csh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = csh
SPEC.loader.exec_module(csh)

RID = "brief-fixture"
DAY = "2026-10-05"
AGENT = "`ogilvy-trends-v2.intelligence_42_agent`"


class Row(dict):
    __getattr__ = dict.get


def critic(**changes):
    base = {"non_cultural_explanation": "a news or scheduled event", "ruled_out": False, "news_driven": False,
            "scheduled_event": True, "local_reaction": True, "local_why_now": True, "reason": "fans react"}
    return {**base, **changes}


def held_payload(**item):
    held = {"item_id": "item-fixture", "title": "Topic fixture", "rule": "G10", "reason": "explanation_failed",
            "reason_text": "x", "evidence": [], **item}
    return {"status": "partial", "held_back": {"items": [held]}, "critic": [{"item_id": "item-fixture", **critic()}]}


class Client:
    def __init__(self, payload=None, checks=(), fail=()):
        self.payload, self.checks, self.fail, self.seen = payload, list(checks), set(fail), []

    def query(self, sql, job_config=None):
        name = ("runs" if "GROUP BY b.run_id" in sql else "payload" if "TO_JSON_STRING(payload)" in sql
                else "checks")
        self.seen.append(name)
        rows = {"runs": [Row(run_id=RID, published_at=dt.datetime(2026, 10, 5, 6, tzinfo=dt.timezone.utc),
                             run_status="ok", markets="NG partial")],
                "payload": [Row(market="NG", p=json.dumps(self.payload))] if self.payload is not None else [],
                "checks": self.checks}[name]

        class Result:
            def result(inner):
                if name in self.fail:
                    raise RuntimeError("boom")
                return rows
        return Result()


def k1(claim_id="c1"):
    return Row(answer_or_brief_id=f"{RID}:NG:item-fixture", claim_id=claim_id, rule="K1", verdict="pass",
               checker="code", reason="Claim checks: passed")


def run(client, argv=("check_support_holds.py", DAY), capsys=None):
    code = csh.main(list(argv), c=client)
    return code, capsys.readouterr().out


def test_a_failed_payload_read_stops_the_report_instead_of_printing_an_empty_brief(capsys):
    code, out = run(Client(held_payload(), fail=("payload",)), capsys=capsys)
    assert code == 1 and "payload read failed" in out
    assert "no briefs row in this run" not in out


def test_a_failed_claim_checks_read_shows_the_checks_as_unread_not_as_none(capsys):
    code, out = run(Client(held_payload(), fail=("checks",)), capsys=capsys)
    assert "checks: ? (claim_checks not read)" in out
    assert "none (no explanation ran" not in out


def test_the_date_is_required_and_never_defaults_to_a_past_day(capsys):
    client = Client(held_payload())
    code = csh.main(["check_support_holds.py"], c=client)
    assert code == 2
    assert client.seen == []
    assert "usage" in capsys.readouterr().err.lower()


class DuckAgentClient:
    """Runs the script's own SQL text on DuckDB over agent.briefs and agent.runs fixtures."""

    def __init__(self, con):
        self.con, self.sql = con, []

    def query(self, sql, job_config=None):
        from core.detect.tests import duck
        self.sql.append(sql)
        params = {p.name: duck._value(p) for p in (job_config.query_parameters if job_config else [])}
        return duck._Job([Row(r) for r in duck.query(self.con, sql.replace(AGENT, "agent"), params)])


def brief_runs_world(order):
    from core.detect.tests import duck
    day = dt.date(2026, 10, 5)
    t0 = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.timezone.utc)
    briefs = [{"brief_date": day, "market": "NG", "run_id": "brief-a", "published_at": t0, "status": "published",
               "payload": json.dumps({})},
              {"brief_date": day, "market": "ZA", "run_id": "brief-b", "published_at": t0, "status": "published",
               "payload": json.dumps({})}]
    runs = [{"run_id": "brief-a", "stage": "brief", "run_date": day, "status": "running", "started_at": t0},
            {"run_id": "brief-a", "stage": "brief", "run_date": day, "status": "ok", "started_at": t0,
             "finished_at": t0 + dt.timedelta(minutes=5)},
            {"run_id": "brief-b", "stage": "brief", "run_date": day, "status": "running", "started_at": t0},
            {"run_id": "brief-b", "stage": "brief", "run_date": day, "status": "failed", "started_at": t0,
             "finished_at": t0 + dt.timedelta(minutes=2)}]
    con = duck.connect(views=False)
    duck.load(con, "agent.briefs", briefs)
    duck.load(con, "agent.runs", runs[::-1] if order == "reversed" else runs)
    return con


@pytest.mark.parametrize("order", ["forward", "reversed"])
def test_the_run_status_is_the_terminal_state_whatever_order_the_rows_arrive_in(order, capsys):
    csh.main(["check_support_holds.py", DAY], c=DuckAgentClient(brief_runs_world(order)))
    out = capsys.readouterr().out
    assert "brief-a" in out and "run ok" in out and "run failed" in out
    assert "run running" not in out
    assert [l for l in out.splitlines() if l.startswith("== reading run")] == ["== reading run brief-b"]
