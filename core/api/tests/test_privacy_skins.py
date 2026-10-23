"""Current suppression on client skins (contract 15.1 and 16): a skin saved while a person was visible keeps their
handle in its stored row, and every reader masks it with the list read now. A write or a report plan that would name
a hidden person is refused, and the stored row is never changed by a read."""
import copy
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, auth, privacy, skins, today
from core.api import app as api_mod
from core.api import store as store_mod

PASS = "s3cret-passcode"
GOOD = {"X-Passcode": PASS}
SKIN_ID = "sk_0123456789ab"
HIDDEN = ({"x:fixture_hidden"}, set(), set())
NOBODY = (set(), set(), set())


def stored_row():
    """A skin saved before @fixture_hidden was hidden, in every user-typed field and as an approved account."""
    return {"skin_id": SKIN_ID, "skin_key": "bsa", "created_at": "2026-10-01T09:00:00+02:00",
            "status_at": "2026-10-01T09:00:00+02:00", "who": "passcode", "name": "Brand SA and @fixture_hidden",
            "markets": json.dumps(["ZA"]), "terms": json.dumps(["@fixture_hidden", "taxi fares"]),
            "hashtags": json.dumps(["#fixture_hidden"]),
            "accounts": json.dumps([{"platform": "x", "handle": "@fixture_hidden", "org": "Brand South Africa",
                                     "role": "client"}]),
            "watch_ids": json.dumps([]), "template": "weekly_report", "status": "active"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.setenv("F42_AGENT", "fixture")
    for name in ("F42_DATA", "F42_PROJECT", "AGENT_URL", "F42_AUTH_MODE", "IAP_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)
    monkeypatch.setattr(core.api, "store", store_mod, raising=False)
    monkeypatch.setitem(sys.modules, "core.collect.gdelt", types.SimpleNamespace(blocked=lambda text: False))
    path = tmp_path / "skin_accounts.yaml"
    path.write_text(json.dumps({"bsa": [{"platform": "x", "handle": "@fixture_hidden", "org": "Brand South Africa",
                                         "role": "client"},
                                        {"platform": "x", "handle": "@BrandSA", "org": "Brand South Africa",
                                         "role": "client"}]}), encoding="utf-8")
    monkeypatch.setattr(skins, "ACCOUNTS_FILE", path)
    hidden = {"now": HIDDEN}
    monkeypatch.setattr(privacy, "read_hidden", lambda store: hidden["now"])
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    agent_app.SKINS.clear()
    agent_app.SKINS.append(stored_row())
    yield types.SimpleNamespace(agent=TestClient(agent_app.app), api=TestClient(api_mod.app), hidden=hidden)
    agent_app.SKINS.clear()


def clear_of_handle(text):
    return "fixture_hidden" not in text.casefold()


def test_the_list_and_detail_mask_a_later_hidden_handle_and_leave_the_row(world):
    before = copy.deepcopy(agent_app.SKINS)
    for client, headers in ((world.agent, {}), (world.api, GOOD)):
        listed = client.get("/api/skins", headers=headers)
        one = client.get(f"/api/skins/{SKIN_ID}", headers=headers)
        assert listed.status_code == 200 and one.status_code == 200
        assert clear_of_handle(listed.text) and clear_of_handle(one.text)
        assert one.json()["skin_id"] == SKIN_ID and one.json()["terms"][1] == "taxi fares"
        assert one.json()["name"] == "Brand SA and @***"
    assert agent_app.SKINS == before


def test_an_archive_answer_masks_the_handle_too(world):
    r = world.api.post(f"/api/skins/{SKIN_ID}/archive", headers=GOOD)
    assert r.status_code == 200 and r.json()["status"] == "archived"
    assert clear_of_handle(r.text)
    assert "fixture_hidden" in agent_app.SKINS[-1]["name"]  # the stored row keeps what was saved


def test_a_reader_with_no_list_masks_every_handle(world):
    world.hidden["now"] = None
    one = world.api.get(f"/api/skins/{SKIN_ID}", headers=GOOD)
    assert one.status_code == 200 and "@fixture_hidden" not in one.text


def test_today_for_the_skin_does_not_name_the_hidden_handle(world, monkeypatch):
    monkeypatch.setattr(today, "build_today", lambda store, date=None: {"markets": [], "headline": None})
    r = world.api.get(f"/api/skins/{SKIN_ID}/today", headers=GOOD)
    assert r.status_code == 200, r.text
    assert r.json()["skin"]["name"] == "Brand SA and @***"
    assert clear_of_handle(r.text)


def body(**change):
    out = {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA"], "terms": ["taxi fares"],
           "hashtags": [], "accounts": [{"platform": "x", "handle": "@BrandSA"}], "watch_ids": [],
           "template": "weekly_report"}
    out.update(change)
    return out


@pytest.mark.parametrize("change", [
    {"name": "Brand SA and @fixture_hidden"}, {"terms": ["fixture_hidden fans"]}, {"hashtags": ["#fixture_hidden"]},
    {"accounts": [{"platform": "x", "handle": "@fixture_hidden"}]},
])
def test_a_write_that_names_a_hidden_person_is_refused(world, change):
    before = copy.deepcopy(agent_app.SKINS)
    for r in (world.agent.post("/api/skins", json=body(**change)),
              world.agent.put(f"/api/skins/{SKIN_ID}", json=body(**change))):
        assert r.status_code == 400 and r.json()["error"] == "bad_request"
        assert clear_of_handle(r.text)
    assert agent_app.SKINS == before


def test_a_write_waits_for_the_list(world):
    world.hidden["now"] = None
    r = world.agent.post("/api/skins", json=body())
    assert r.status_code == 503 and len(agent_app.SKINS) == 1
    world.hidden["now"] = NOBODY
    assert world.agent.post("/api/skins", json=body()).status_code == 201


def test_a_report_plan_for_a_skin_that_names_a_hidden_person_is_refused(world, monkeypatch):
    planned = []
    monkeypatch.setattr(agent_app, "resolve_investigator", lambda: (lambda *a, **k: planned.append(a), None))
    r = agent_app.draft_investigation({"question": "Weekly report", "market": "ZA", "skin_id": SKIN_ID})
    assert r.status_code == 409 and clear_of_handle(r.body.decode())
    world.hidden["now"] = None
    r = agent_app.draft_investigation({"question": "Weekly report", "market": "ZA", "skin_id": SKIN_ID})
    assert r.status_code == 503
    assert planned == []
