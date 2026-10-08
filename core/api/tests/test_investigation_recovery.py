import copy
import json
import socket

import pytest
from fastapi.testclient import TestClient

from core.api import agent_app
from core.api.tests.test_agent_app import (client, draft, env, inv, use_gate)  # noqa: F401

pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original = socket.socket.connect

    def blocked(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original(sock, address)
        raise AssertionError("Network access is forbidden in recovery tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def lose_finish(client, inv, monkeypatch, status="complete"):
    gate = use_gate(monkeypatch, inv)
    question = "Why does this fail for #fixture?" if status == "failed" else "What is behind #fixture?"
    inv_id = draft(client, question).json()["investigation_id"]
    original = agent_app.append

    def append(table, held, row):
        if table == "investigations" and row["status"] in ("complete", "stopped", "failed"):
            return False
        return original(table, held, row)

    monkeypatch.setattr(agent_app, "append", append)
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    ask = agent_app.get_ask(started["ask_id"])
    if status == "stopped":
        ask.stop = True
    gate.opened.set()
    assert ask.finished.wait(5)
    assert agent_app.INVESTIGATIONS[-1]["status"] == "running"
    assert json.loads(agent_app.SINK[-1]["record"])["status"] == status
    monkeypatch.setattr(agent_app, "append", original)
    return inv_id, started["ask_id"], gate


@pytest.mark.parametrize("status", ["complete", "stopped", "failed"])
def test_read_recovers_a_lost_terminal_write_without_research(client, inv, monkeypatch, status):
    inv_id, ask_id, gate = lose_finish(client, inv, monkeypatch, status)
    before = copy.deepcopy(agent_app.INVESTIGATIONS)
    body = client.get(f"/api/investigations/{inv_id}").json()
    assert body["status"] == status
    assert body["record"]["status"] == status
    assert body["ask_id"] == ask_id
    expected_run_id = "r_plan_" + inv_id[2:] if status == "failed" else "r_" + ask_id[2:]
    assert body["run_id"] == expected_run_id
    assert agent_app.INVESTIGATIONS[:-1] == before
    assert agent_app.INVESTIGATIONS[-1]["version"] == 3
    client.get(f"/api/investigations/{inv_id}")
    assert len(agent_app.INVESTIGATIONS) == 3
    assert len(gate.requests) == 1


def test_startup_recovers_from_durable_ask_after_memory_is_lost(client, inv, monkeypatch):
    inv_id, ask_id, gate = lose_finish(client, inv, monkeypatch)
    agent_app.ASKS.clear()
    agent_app.RUNNING_INVESTIGATIONS.clear()
    inv.RESERVED.clear()
    with TestClient(agent_app.app) as restarted:
        assert agent_app.INVESTIGATIONS[-1]["status"] == "complete"
        body = restarted.get(f"/api/investigations/{inv_id}").json()
        assert body["ask_id"] == ask_id and body["record"]["answer"]["claims"]
        assert len(gate.requests) == 1


def test_recovery_write_failure_keeps_running_and_retries_later(client, inv, monkeypatch):
    inv_id, _, gate = lose_finish(client, inv, monkeypatch)
    original = agent_app.append
    monkeypatch.setattr(agent_app, "append", lambda *args: False)
    assert client.get(f"/api/investigations/{inv_id}").json()["status"] == "running"
    assert len(agent_app.INVESTIGATIONS) == 2
    monkeypatch.setattr(agent_app, "append", original)
    assert client.get(f"/api/investigations/{inv_id}").json()["status"] == "complete"
    assert len(gate.requests) == 1


def test_another_worker_with_no_terminal_ask_is_preserved(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client).json()["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    ask = agent_app.get_ask(started["ask_id"])
    before = copy.deepcopy(agent_app.INVESTIGATIONS)
    with agent_app._lock:
        agent_app.ASKS.clear()
        agent_app.RUNNING_INVESTIGATIONS.clear()
    with TestClient(agent_app.app) as restarted:
        assert restarted.get(f"/api/investigations/{inv_id}").json()["status"] == "running"
        assert agent_app.INVESTIGATIONS == before
        assert not ask.stop
        with agent_app._lock:
            agent_app.ASKS[started["ask_id"]] = ask
            agent_app.RUNNING_INVESTIGATIONS[inv_id] = ask
        gate.opened.set()
        assert ask.finished.wait(5)


def test_filtered_list_moves_recovered_run_to_terminal_status(client, inv, monkeypatch):
    inv_id, _, _ = lose_finish(client, inv, monkeypatch)
    assert client.get("/api/investigations?status=running").json()["investigations"] == []
    complete = client.get("/api/investigations?status=complete").json()["investigations"]
    assert [row["investigation_id"] for row in complete] == [inv_id]


def test_terminal_memory_without_a_durable_ask_does_not_recover(client, inv, monkeypatch):
    inv_id, _, _ = lose_finish(client, inv, monkeypatch)
    agent_app.SINK.clear()
    assert client.get(f"/api/investigations/{inv_id}").json()["status"] == "running"
    assert len(agent_app.INVESTIGATIONS) == 2


def test_a_terminal_record_for_a_different_investigation_is_not_used(client, inv, monkeypatch):
    inv_id, _, _ = lose_finish(client, inv, monkeypatch)
    record = json.loads(agent_app.SINK[-1]["record"])
    record["investigation_id"] = "i_000000000000"
    agent_app.SINK[-1]["record"] = json.dumps(record)
    agent_app.ASKS.clear()
    assert client.get(f"/api/investigations/{inv_id}").json()["status"] == "running"
    assert len(agent_app.INVESTIGATIONS) == 2


# L3 server side: an investigation whose single owning process died stayed 'running' forever, so the page polled it
# every 2 s and Stop answered "is running, not running".
def orphan(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client).json()["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    ask = agent_app.get_ask(started["ask_id"])
    with agent_app._lock:
        agent_app.ASKS.clear()
        agent_app.RUNNING_INVESTIGATIONS.clear()
    return inv_id, ask, gate


def run_age(monkeypatch, inv_id, extra_seconds):
    """The clock moved to extra_seconds past the end of the longest the run may take."""
    from datetime import datetime, timedelta
    row = agent_app.INVESTIGATIONS[-1]
    started = datetime.fromisoformat(row["created_at"])
    minutes = json.loads(row["estimate"])["minutes"]
    monkeypatch.setattr(agent_app, "_utcnow", lambda: started + timedelta(
        seconds=agent_app.ORPHAN_AFTER_SECONDS + minutes * 60 + extra_seconds), raising=False)


def test_a_run_nobody_holds_and_no_record_exists_for_is_closed_once_it_is_past_its_longest_time(client, inv, monkeypatch):
    inv_id, ask, gate = orphan(client, inv, monkeypatch)
    run_age(monkeypatch, inv_id, 1)
    with TestClient(agent_app.app) as restarted:
        body = restarted.get(f"/api/investigations/{inv_id}").json()
        assert body["status"] == "failed"
        assert agent_app.INVESTIGATIONS[-1]["status"] == "failed"
        assert agent_app.INVESTIGATIONS[-1]["version"] == agent_app.INVESTIGATIONS[-2]["version"] + 1
        stop = restarted.post(f"/api/investigations/{inv_id}/stop")
        assert "running, not running" not in stop.text
    gate.opened.set()
    assert ask.finished.wait(5)


def test_a_run_nobody_holds_is_left_running_until_it_is_past_its_longest_time(client, inv, monkeypatch):
    inv_id, ask, gate = orphan(client, inv, monkeypatch)
    run_age(monkeypatch, inv_id, -60)
    before = copy.deepcopy(agent_app.INVESTIGATIONS)
    with TestClient(agent_app.app) as restarted:
        assert restarted.get(f"/api/investigations/{inv_id}").json()["status"] == "running"
        assert agent_app.INVESTIGATIONS == before
    gate.opened.set()
    assert ask.finished.wait(5)


def test_stopping_a_run_nobody_holds_does_not_call_it_running_and_not_running(client, inv, monkeypatch):
    inv_id, ask, gate = orphan(client, inv, monkeypatch)
    with TestClient(agent_app.app) as restarted:
        stop = restarted.post(f"/api/investigations/{inv_id}/stop")
        assert stop.status_code == 409 and "running, not running" not in stop.text
    gate.opened.set()
    assert ask.finished.wait(5)
