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
    agent_app.REQUESTS.clear()
    agent_app.STOPPED_REQUESTS.clear()
    yield
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    agent_app.REQUESTS.clear()
    agent_app.STOPPED_REQUESTS.clear()


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


# A start sent again with its request_id (audit W04-04), and a stop sent before the start answered (audit W20-03).

def workers():
    return sorted(t.name for t in threading.enumerate() if t.name.startswith("a_"))


def test_a_start_sent_again_after_a_lost_reply_gets_the_same_ask(client, monkeypatch):
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.2")
    first = ask(client, request_id="req-0123456789abcdef")
    assert first.status_code == 202  # the reply the proxy lost
    again = ask(client, request_id="req-0123456789abcdef")
    assert again.status_code == 202
    assert again.json()["ask_id"] == first.json()["ask_id"] and again.json()["repeat"] is True
    assert list(agent_app.ASKS) == [first.json()["ask_id"]]
    assert workers() == [first.json()["ask_id"]]
    other = ask(client, request_id="req-fedcba9876543210")
    assert other.json()["ask_id"] != first.json()["ask_id"] and "repeat" not in other.json()


def test_a_waiting_start_sent_again_returns_the_first_asks_record(client):
    first = ask(client, request_id="req-0123456789abcdef", wait=True).json()
    again = ask(client, request_id="req-0123456789abcdef", wait=True)
    assert again.status_code == 200 and again.json()["ask_id"] == first["ask_id"]
    assert len(agent_app.ASKS) == 1


def test_a_bad_request_id_is_refused(client):
    for bad in ("short", "x" * 65, "has space here", 12345678):
        assert ask(client, request_id=bad).status_code == 400
    assert client.post("/api/ask/requests/short/stop").status_code == 400


def test_a_stop_before_the_start_arrives_refuses_that_start(client):
    r = client.post("/api/ask/requests/req-0123456789abcdef/stop")
    assert r.status_code == 202 and r.json() == {"request_id": "req-0123456789abcdef", "ask_id": None,
                                                 "status": "stopped"}
    late = ask(client, request_id="req-0123456789abcdef")
    assert late.status_code == 409 and late.json()["error"] == "stopped"
    assert agent_app.ASKS == {} and workers() == []


def test_a_stop_by_request_id_stops_the_ask_its_start_made(client, monkeypatch):
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.2")
    started = ask(client, request_id="req-0123456789abcdef").json()  # the page has not read this reply yet
    r = client.post("/api/ask/requests/req-0123456789abcdef/stop")
    assert r.status_code == 202 and r.json()["ask_id"] == started["ask_id"] and r.json()["status"] == "stopping"
    held = agent_app.get_ask(started["ask_id"])
    assert held.finished.wait(5)
    assert held.snapshot()["status"] == "stopped"
    # The ask stays discoverable by its request id: the same start returns it, stopped, and starts nothing new.
    again = ask(client, request_id="req-0123456789abcdef")
    assert again.status_code == 202 and again.json()["ask_id"] == started["ask_id"]
    assert again.json()["status"] == "stopped" and len(agent_app.ASKS) == 1
