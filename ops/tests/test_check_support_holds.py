"""check_support_holds on fake warehouse rows: no cloud client, no BigQuery."""
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path


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


def test_a_scheduled_event_critic_answer_shows_its_scheduled_event_field(capsys):
    code, out = run(Client(held_payload(), checks=[k1()]), capsys=capsys)
    [line] = [l for l in out.splitlines() if l.startswith("  critic answer:")]
    assert "scheduled_event True" in line
    assert "news_driven False" in line and "local_reaction True" in line
