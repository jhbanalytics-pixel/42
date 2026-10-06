"""The investigation routes against the agent's own rules: what a draft checks before any spend is booked, and the
plan start hands to run_ask (core/agent/investigate.py validate_plan is authoritative)."""
import json

import pytest

from core.api import agent_app
from core.api.tests.test_agent_app import (client, draft, env, inv, new_skin, skin_env,  # noqa: F401 (fixtures)
                                           use_gate, wait_finished)

pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

PLAN_KEYS = {"sub_questions", "researchers", "gap_round", "max_credits", "max_model_usd"}


def planner_rows():
    return [r for r in agent_app.SINK if r["stage"] in ("investigation", "investigation_spend")]


@pytest.mark.parametrize("angle", ["ab", "  a  ", "x" * 301])
def test_a_draft_refuses_angles_the_planner_would_refuse_before_any_spend(client, inv, monkeypatch, angle):
    called = []
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (lambda r: called.append(r) or inv.fixture_planner(r), inv.fixture_estimate, None))
    r = draft(client, angles=["Where it started", angle])
    assert r.status_code == 400
    assert "3 to 300 characters" in r.json()["message"]
    assert called == [] and planner_rows() == [] and agent_app.INVESTIGATIONS == []
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


@pytest.mark.parametrize("angle", ["abc", "y" * 250, "z" * 300])
def test_a_draft_takes_angles_the_planner_takes(client, inv, monkeypatch, angle):
    seen = []
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (lambda r: seen.append(r) or inv.fixture_planner(r), inv.fixture_estimate, None))
    r = draft(client, angles=[angle])
    assert r.status_code == 201, r.text
    assert seen[0]["angles"] == [angle]


def store_plan(inv_id, change):
    row = [r for r in agent_app.INVESTIGATIONS if r["investigation_id"] == inv_id][-1]
    row["plan"] = json.dumps(change(json.loads(row["plan"])))


def test_start_hands_run_ask_only_the_plan_and_keeps_the_skin_stored(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    store_plan(inv_id, lambda plan: {**plan, "skin_id": "sk_0123456789ab"})
    r = client.post(f"/api/investigations/{inv_id}/start")
    assert r.status_code == 202, r.text
    sent = gate.requests[0]["plan"]
    assert set(sent) == PLAN_KEYS
    assert agent_app.get_ask(r.json()["ask_id"]).record["skin_id"] == "sk_0123456789ab"
    running = [row for row in agent_app.INVESTIGATIONS if row["status"] == "running"]
    assert json.loads(running[0]["plan"])["skin_id"] == "sk_0123456789ab"
    gate.opened.set()
    finished = wait_finished(inv_id)
    assert json.loads(finished["plan"])["skin_id"] == "sk_0123456789ab"


def test_a_running_skin_investigation_still_names_its_skin_for_a_dossier(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    seen = []
    monkeypatch.setattr(agent_app, "skin_people", lambda skin_id, record=None: seen.append(skin_id) or {})
    monkeypatch.setattr(agent_app, "first_version", lambda record, change, source, people: people)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    store_plan(inv_id, lambda plan: {**plan, "skin_id": "sk_0123456789ab"})
    assert client.post(f"/api/investigations/{inv_id}/start").status_code == 202
    monkeypatch.setattr(agent_app, "investigation_record", lambda ask_id: {"status": "complete"})
    agent_app.create_dossier_from_investigation(inv_id, {})
    gate.opened.set()
    wait_finished(inv_id)
    assert seen == ["sk_0123456789ab"]


def test_start_hands_run_ask_legacy_platform_names_as_agent_groups(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client).json()["investigation_id"]

    def legacy(plan):
        plan["sub_questions"][0]["platforms"] = ["twitter", "reddit"]
        return plan

    store_plan(inv_id, legacy)
    assert client.post(f"/api/investigations/{inv_id}/start").status_code == 202
    assert gate.requests[0]["plan"]["sub_questions"][0]["platforms"] == ["x", "reddit_threads"]
    gate.opened.set()
    wait_finished(inv_id)


def test_start_refuses_a_stored_plan_the_agent_cannot_run(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client).json()["investigation_id"]

    def telegram(plan):
        plan["sub_questions"][0]["platforms"] = ["telegram"]
        return plan

    store_plan(inv_id, telegram)
    r = client.post(f"/api/investigations/{inv_id}/start")
    assert r.status_code == 409 and "Telegram" in r.json()["message"]
    assert gate.requests == [] and agent_app.ASKS == {}
    assert [row["status"] for row in agent_app.INVESTIGATIONS] == ["draft"]
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
