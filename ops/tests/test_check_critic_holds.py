"""Critic report counts on fake warehouse rows, without creating a cloud client."""
import datetime as dt
import json
import runpy
import sys
from pathlib import Path

import pytest
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


NEWS_PASS = "Critic: news-driven, local creators react in their own words"
EVENT_PASS = "Critic: event-driven, local creators react in their own words"
FIRST_DRAFT_CUT = "before repair: Critic: local why-now not shown"
FIRST_DRAFT_NOT_RULED_OUT = "before repair: Critic: a simpler explanation was not ruled out; local why-now not shown"


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


def critic_row(reason, rid="brief-fixture"):
    return Row(answer_or_brief_id=f"{rid}:NG:item-fixture", claim_id=None, rule="critic", verdict="pass",
               checker="model", reason=reason)


def critic_line(out):
    [line] = [line for line in out.splitlines() if line.startswith("  critic: ")]
    return line


def test_an_event_driven_pass_is_read_as_a_pass_not_as_unknown(monkeypatch, capsys):
    out = run_report(monkeypatch, capsys, [critic_row(EVENT_PASS + ": a news or scheduled event")])
    line = critic_line(out)
    assert "verdict pass" in line and "verdict ?" not in line
    assert "event path met" in line and "event path met (scheduled_event and local_reaction and 2+ reacting) True" in line
    assert "news path met (news_driven and local_reaction and 2+ reacting) False" in line
    assert "named, as menu item: a news or scheduled event" in line


@pytest.mark.parametrize("flip", [False, True])
def test_the_final_critic_row_is_read_whatever_order_the_warehouse_returns_the_rows_in(monkeypatch, capsys, flip):
    rows = [critic_row(FIRST_DRAFT_NOT_RULED_OUT), critic_row(NEWS_PASS)]
    out = run_report(monkeypatch, capsys, rows[::-1] if flip else rows)
    line = critic_line(out)
    assert "verdict pass" in line and "news path met (news_driven and local_reaction and 2+ reacting) True" in line
    assert "critic reason (fixed wording): " + NEWS_PASS in out
    [first] = [l for l in out.splitlines() if l.startswith("  first-draft critic reason (fixed wording): ")]
    assert "a simpler explanation was not ruled out" in first and "before repair: " not in first
    assert "before repair" not in line


def test_a_critic_row_from_before_the_repair_alone_is_not_taken_for_the_final_verdict(monkeypatch, capsys):
    out = run_report(monkeypatch, capsys, [critic_row(FIRST_DRAFT_CUT)])
    line = critic_line(out)
    assert "no final-draft critic row" in line and "verdict" not in line
    assert any(l.startswith("  first-draft critic (before repair): verdict cut") for l in out.splitlines())
