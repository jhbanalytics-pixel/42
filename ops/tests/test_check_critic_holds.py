"""Critic report counts on fake warehouse rows, without creating a cloud client."""
import datetime as dt
import json
import runpy
import sys
from pathlib import Path

from google.cloud import bigquery


class Row(dict):
    __getattr__ = dict.get


def test_other_cuts_exclude_title_downgrade_and_nonterminal_rows(monkeypatch, capsys):
    rid = "brief-fixture"
    answer = f"{rid}:NG:item-fixture"
    checks = [Row(answer_or_brief_id=answer, claim_id=None, rule=rule, verdict=verdict, checker="fixture", reason=reason)
              for rule, verdict, reason in [("support", "cut", "actual cut"), ("title", "cut", "title only"),
                                            ("K3", "downgrade", "downgrade only"), ("K4", "pending", "not terminal"),
                                            ("K1", "breach", "breach only"),
                                            ("support", "cut", "before repair: old cut")]]
    payload = {"status": "partial", "held_back": {"items": [
        {"item_id": "item-fixture", "title": "Topic fixture", "evidence": []}]}}
    answers = [[Row(run_id=rid, published_at=dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc),
                    run_status="ok", markets="NG partial")], [Row(market="NG", p=json.dumps(payload))], checks,
               [], [], []]

    class Client:
        def query(self, sql, job_config=None):
            rows = answers.pop(0)
            return type("Result", (), {"result": lambda self: rows})()

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: Client())
    monkeypatch.setattr(sys, "argv", ["check_critic_holds.py", "2026-10-05", rid])
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "runners" / "check_critic_holds.py"))
    out = capsys.readouterr().out
    [line] = [line for line in out.splitlines() if "other cuts after repair:" in line]
    assert "support actual cut x1" in line
    assert "title only" not in line and "downgrade only" not in line and "not terminal" not in line
    assert "breach only" not in line
    assert "(1 before-repair rows)" in line
    assert answers == []
