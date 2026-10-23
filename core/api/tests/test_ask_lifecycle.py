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
