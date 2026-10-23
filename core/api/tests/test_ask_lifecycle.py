"""The Ask lifecycle on f42-agent: admission, the worker launch, and retries of the same request (audit W04)."""
import threading

import pytest
from fastapi.testclient import TestClient

from core.api import agent_app

pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.01")
    monkeypatch.delenv("F42_DATA", raising=False)
    monkeypatch.delenv("F42_PROJECT", raising=False)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()


@pytest.fixture(autouse=True)
def nobody_hidden(monkeypatch):
    from core.api import privacy
    monkeypatch.setattr(privacy, "read_hidden", lambda store: (set(), set(), set()))


@pytest.fixture
def client():
    return TestClient(agent_app.app)


def ask(client, **body):
    return client.post("/api/ask", json={"question": "What is behind #fixture in South Africa this week?",
                                         "market": "ZA", **body})


def running():
    with agent_app._lock:
        return sum(1 for a in agent_app.ASKS.values() if a.ended is None)


def test_a_worker_that_cannot_start_leaves_no_running_ask(client, monkeypatch):
    """W04-01: a Thread.start failure returns a controlled error and gives the admission slot back."""
    real = threading.Thread.start

    def refuse(self):
        if self.name.startswith("a_"):
            raise RuntimeError("can't start new thread")
        return real(self)

    monkeypatch.setattr(threading.Thread, "start", refuse)
    before = running()
    r = ask(client)
    assert r.status_code == 503
    assert r.json()["error"] == "agent_unavailable"
    assert agent_app.ASKS == {}
    assert running() == before

    monkeypatch.setattr(threading.Thread, "start", real)
    later = ask(client, wait=True)
    assert later.status_code == 200 and later.json()["status"] == "complete"


# Dossiers made from an Ask (audit W21-01 and W21-02).

from core.api.tests.test_agent_app import dossier_store  # noqa: E402,F401  the source Ask, read back from runs


@pytest.mark.parametrize("failure", ["row_errors", "raises"])
def test_no_dossier_is_made_from_an_ask_whose_run_was_not_recorded(client, dossier_store, monkeypatch, failure):
    """W21-01: the held Ask carries NOT_RECORDED, so once it leaves this process nothing could edit or freeze it."""
    def unrecorded(held):
        if failure == "raises":
            raise RuntimeError("insert failed")
        return False

    monkeypatch.setattr(agent_app, "sink", unrecorded)
    record = ask(client, wait=True).json()
    assert agent_app.NOT_RECORDED in agent_app.get_ask(record["ask_id"]).snapshot()["run"]["notices"]
    r = client.post("/api/dossiers", json={"from": {"ask_id": record["ask_id"]}})
    assert r.status_code == 409 and r.json()["error"] == "not_recorded"
    assert agent_app.DOSSIER_VERSIONS == []


def test_a_recorded_ask_still_makes_a_dossier(client, dossier_store):
    record = ask(client, wait=True).json()
    assert client.post("/api/dossiers", json={"from": {"ask_id": record["ask_id"]}}).status_code == 201


def lost_reply(monkeypatch):
    """An append that stores its row and then raises, as a BigQuery insert whose reply is lost does."""
    real = agent_app.append
    calls = []

    def append(table, held, row):
        calls.append(table)
        real(table, held, row)
        if len(calls) == 1:
            raise ConnectionError("the insert reply was lost")
        return True

    monkeypatch.setattr(agent_app, "append", append)
    return calls


def test_a_retried_dossier_create_returns_the_dossier_already_stored(client, dossier_store, monkeypatch):
    """W21-02: the first insert lands and its reply is lost; the retry with the same request id gets that dossier."""
    lost_reply(monkeypatch)
    body = {"from": {"ask_id": dossier_store["record"]["ask_id"]}, "request_id": "req-0123456789abcdef"}
    first = client.post("/api/dossiers", json=body)
    assert first.status_code == 201
    again = client.post("/api/dossiers", json=body)
    assert again.status_code == 200
    assert again.json()["dossier_id"] == first.json()["dossier_id"]
    assert [(r["dossier_id"], r["version"]) for r in agent_app.DOSSIER_VERSIONS] == [(first.json()["dossier_id"], 1)]


def test_a_dossier_create_whose_insert_did_not_land_is_not_confirmed(client, dossier_store, monkeypatch):
    def append(table, held, row):
        raise ConnectionError("the insert never arrived")

    monkeypatch.setattr(agent_app, "append", append)
    body = {"from": {"ask_id": dossier_store["record"]["ask_id"]}, "request_id": "req-0123456789abcdef"}
    r = client.post("/api/dossiers", json=body)
    assert r.status_code == 503 and r.json()["error"] == "save_unconfirmed"


def test_different_request_ids_make_different_dossiers(client, dossier_store):
    source = {"ask_id": dossier_store["record"]["ask_id"]}
    one = client.post("/api/dossiers", json={"from": source, "request_id": "req-aaaaaaaaaaaa"}).json()
    two = client.post("/api/dossiers", json={"from": source, "request_id": "req-bbbbbbbbbbbb"}).json()
    assert one["dossier_id"] != two["dossier_id"]
    assert client.post("/api/dossiers", json={"from": source, "request_id": "x"}).status_code == 400
