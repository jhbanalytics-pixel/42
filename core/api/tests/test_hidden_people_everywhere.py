"""A hidden person stays hidden on every read (SETUP.md data protection, contract.md section 16). Before 16 October
2026 three gaps let a handle through: strings under "label" were never masked, the watch, schedule and alert lists
skipped the mask, and Today, Discover, Topic and Compare read a narrower hidden list than Ask and History."""
import pytest
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, auth, privacy, today
from core.api import app as api_mod
from core.api import store as store_mod

PASS = "s3cret-passcode"
GOOD = {"X-Passcode": PASS}
HANDLE = "fixture_ng_hidden"  # suppressed in the fixtures


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.delenv("F42_PROJECT", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.setattr(store_mod, "_CATALOG", {})
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)
    monkeypatch.setattr(core.api, "store", store_mod, raising=False)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    store_mod.SUPPRESSIONS.clear()
    yield
    store_mod.SUPPRESSIONS.clear()


def test_a_label_naming_a_hidden_person_is_masked():
    hidden = privacy.read_hidden(store_mod.get_store())
    body = {"items": [{"item_id": "abc", "kind": "topic", "label": f"@{HANDLE} dance challenge"}],
            "alerts": [{"watch_id": "w_1", "label": f"Posts by @{HANDLE}", "market": "NG"}]}
    out = privacy.mask(body, hidden)
    assert out["items"][0]["label"] == "@*** dance challenge"
    assert out["alerts"][0]["label"] == "Posts by @***"
    assert (out["items"][0]["kind"], out["alerts"][0]["market"]) == ("topic", "NG")  # structure stays


def test_labels_without_a_hidden_person_are_unchanged():
    hidden = privacy.read_hidden(store_mod.get_store())
    body = {"label": "Amapiano Fridays", "items": [{"label": "TikTok"}]}
    assert privacy.mask(body, hidden) == body


def test_watch_and_schedule_lists_mask_a_hidden_handle():
    api = TestClient(api_mod.app)
    question = f"What is @{HANDLE} posting this week?"
    assert api.post("/api/schedules", headers=GOOD, json={
        "question": question, "market": "NG", "tier": "T0", "cadence": "daily", "deliver": ["in_app"]}).status_code == 201
    assert api.post("/api/watches", headers=GOOD, json={
        "target": {"kind": "creator", "value": f"@{HANDLE}"}, "market": "NG",
        "rule": {"state_in": ["rising"]}}).status_code == 201
    schedules = api.get("/api/schedules", headers=GOOD).text
    watches = api.get("/api/watches", headers=GOOD).text
    alerts = api.get("/api/alerts", headers=GOOD)
    assert HANDLE not in schedules and "@***" in schedules
    assert HANDLE not in watches
    assert alerts.status_code == 200 and HANDLE not in alerts.text


def test_a_hide_by_handle_reaches_today_discover_topic_and_compare():
    store_mod.SUPPRESSIONS.append({
        "suppression_id": "sup_app_000000000001", "status_at": "2026-10-16T00:00:00+02:00", "status": "suppressed",
        "creator_id": None, "platform": "tiktok", "handle": "newperson", "reason": "removal request", "who": "Thandi"})
    store = store_mod.get_store()
    assert today.hidden_people(store)[0] == privacy.read_hidden(store)[0]
    card = {"item_id": "i1", "kind": "topic", "title": "Why @newperson is everywhere",
            "evidence": [{"id": "p1", "platform": "tiktok", "handle": "@newperson", "text": "hi"}]}
    shown = today.without_hidden(card, today.hidden_people(store))
    assert shown["title"] == "Why @*** is everywhere" and shown["evidence"] == []
