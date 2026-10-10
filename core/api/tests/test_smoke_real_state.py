"""The release smoke against a real Ask, with no double (review finding 1).

The other smoke tests feed smoke._state_problem a stand-in: smoke_support replaces answer_state.check_wire with a double
and adds the verified wire value inside the test client. That cannot show whether f42-api ever returns the value. This
test runs the real fixture agent through the real execute and reads the finished Ask through f42-api's own GET, then
hands the body to the smoke exactly as the live smoke would. Nothing between the producer and the smoke is replaced.

Until wave8/api C1 lane 2 (persistence in execute, meta_view on the read routes) is in this tree, nothing produces the
value, the body carries no answer_meta and the smoke refuses it, so the test is an expected failure. The marker is
conditional on the C1 producer and reader modules being absent, so the day the readers arrive the test runs for real and must pass:
there is no marker left to remember to remove."""
import importlib.util
import types

import pytest
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, auth, smoke
from core.api import app as api_mod
from core.api import store as store_mod

GOOD = {"X-Passcode": "s3cret-passcode"}
# Keyed on the two C1 modules existing, not on the name of any function in them: a renamed function cannot flip the test back to an
# expected failure without notice, and the strict marker fails the run if the modules are missing yet the pipeline works.
READERS_ARE_HERE = all(importlib.util.find_spec(name) is not None for name in ("core.agent.answer_state", "core.api.summary_state"))


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret-passcode")
    for name in ("F42_AUTH_MODE", "IAP_AUDIENCE", "IAP_ALLOWED_EMAILS", "AGENT_URL", "AGENT_AUDIENCE", "MODEL_DAILY_USD",
                 "ASK_PER_IP_DAILY", "LP_ALLOW_OPEN_GATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.delenv("F42_DATA", raising=False)
    fixture_store = store_mod.FixtureStore()
    monkeypatch.setattr("core.api.store.get_store", lambda: fixture_store)
    monkeypatch.setattr(core.api, "store", types.SimpleNamespace(get_store=lambda: fixture_store), raising=False)
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)  # the real f42-agent, in process
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield TestClient(api_mod.app)
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.ASKS.clear()
    agent_app.SINK.clear()


@pytest.mark.xfail(not READERS_ARE_HERE, strict=True, reason="needs wave8/api C1 readers")
def test_the_smoke_accepts_a_real_ask_read_through_f42_api_with_no_double(api):
    posted = api.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                      "market": "ZA", "wait": True})
    assert posted.status_code == 200
    body = api.get(f"/api/ask/{posted.json()['ask_id']}", headers=GOOD).json()
    assert smoke._state_problem(body) is None, smoke._state_problem(body)
    ok, evidence = smoke.check_ask_record(body)
    assert ok is True, evidence
