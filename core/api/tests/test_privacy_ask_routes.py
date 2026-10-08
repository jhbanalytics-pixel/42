"""Current suppression on the Ask readers of f42-agent and f42-api (C5 v2 matrix rows A10 to A21, A45).

Each test pins one reader. The hide list is the store's, read on the request (rule R2)."""
import copy
import json
import time
import types

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, auth, privacy
from core.api import app as api_mod
from core.api.tests.test_privacy_projection import (CREATORS, LEAK, P_HID1, P_VIS1, PrivStore, ask_all_hidden,
                                                    ask_record, body_of, ev, leaks)

PASS = "s3cret-passcode"
GOOD = {"X-Passcode": PASS}
ASK = "a_20261007_aaaa0001"


class RouteStore(PrivStore):
    """PrivStore that also holds finished Ask records, as the runs table does."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.records = {}

    def ask_record(self, ask_id):
        return copy.deepcopy(self.records.get(ask_id))

    def ask_history(self, limit, before=None, market=None):
        return [{"ask_id": ASK, "question": "What is @hid_handle doing with amapiano?", "asked_at": "2026-10-07T09:00:00+02:00",
                 "status": "complete", "answer_status": "complete", "market": "ZA"}]


def hold(record, events=()):
    """A finished Ask held by f42-agent, with the events it sent."""
    ask = agent_app.Ask({"ask_id": record["ask_id"], "question": record["question"], "market": "ZA"})
    ask.record = copy.deepcopy(record)
    ask.events = [(kind, {"seq": n, **data}) for n, (kind, data) in enumerate(events, 1)]
    ask.done = {"seq": len(ask.events) + 1, "status": record["status"], "url": f"/api/ask/{record['ask_id']}"}
    ask.ended = time.monotonic()
    ask.finished.set()
    agent_app.ASKS[record["ask_id"]] = ask
    return ask


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.delenv("F42_DATA", raising=False)
    store = RouteStore(hide={"c_hid"})
    monkeypatch.setattr("core.api.store.get_store", lambda: store)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield types.SimpleNamespace(store=store, client=TestClient(agent_app.app))
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.ASKS.clear()
    agent_app.SINK.clear()


def sse(text):
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


EVENTS = [
    ("step", {"text": "Searching TikTok for amapiano from @hid_handle"}),
    ("evidence", {"evidence": ev(P_HID1, "hid_handle", "fixture hidden words one")}),
    ("evidence", {"evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}),
    ("claim", {"claim": {"id": "c2", "text": "The hidden creator led", "evidence_ids": [P_HID1]}}),
    ("claim", {"claim": {"id": "c1", "text": "Amapiano posts rose", "evidence_ids": [P_VIS1]}}),
]


# f42-agent, a finished record it holds (A14, A36).
def test_a_finished_record_the_agent_holds_is_projected_with_the_list_read_now_a14(world):
    hold(ask_record())
    r = world.client.get(f"/api/ask/{ASK}")
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [c["id"] for c in body["answer"]["claims"]] == ["c1", "c4"]
    assert body["privacy"]["withheld"] == {"posts": 1, "claims": 2, "summary": True}
    assert leaks(body, [P_HID1]) == []
    assert agent_app.ASKS[ASK].record["answer"]["claims"][1]["id"] == "c2"  # the held record is not changed


def test_a_hide_made_between_two_reads_shows_on_the_second_f(world):
    world.store.hide.clear()
    hold(ask_record())
    first = world.client.get(f"/api/ask/{ASK}").json()
    assert "privacy" not in first and "hid_handle" in body_of(first)
    world.store.hide.add("c_hid")
    assert "hid_handle" not in body_of(world.client.get(f"/api/ask/{ASK}").json())


def test_an_unreadable_list_gives_the_unavailable_record_u(world):
    world.store.fail = "list"
    hold(ask_record())
    body = world.client.get(f"/api/ask/{ASK}").json()
    assert body["answer"]["claims"] == [] and body["answer"]["evidence"] == []
    assert body["privacy"]["state"] == "unavailable"


# The stream of a run (A10).
def test_the_event_stream_leaves_out_a_hidden_creators_evidence_and_claims_and_masks_its_steps_a10(world):
    hold(ask_record(), EVENTS)
    r = world.client.get(f"/api/ask/{ASK}/events")
    events = sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds.count("evidence") == 1 and kinds.count("claim") == 1 and kinds[-1] == "done"
    assert [d["evidence"]["id"] for k, d in events if k == "evidence"] == [P_VIS1]
    assert [d["claim"]["id"] for k, d in events if k == "claim"] == ["c1"]
    assert leaks(r.text, [P_HID1]) == []


def test_an_unreadable_list_sends_one_step_and_no_evidence_or_claim_a10(world):
    world.store.fail = "list"
    hold(ask_record(), EVENTS)
    events = sse(world.client.get(f"/api/ask/{ASK}/events").text)
    assert [k for k, _ in events if k in ("evidence", "claim")] == []
    notes = [d["text"] for k, d in events if k == "step" and d["text"] == privacy.UNAVAILABLE_NOTE]
    assert len(notes) == 1
    assert events[-1][0] == "done"


def test_a_hide_made_during_the_run_shows_in_its_stream_after_thirty_seconds_case_g(world, monkeypatch):
    now = [10.0]
    monkeypatch.setattr(privacy, "_clock", lambda: now[0])
    world.store.hide.clear()
    ask = hold(ask_record(), EVENTS)
    fresh = privacy.FreshList(world.store)
    assert privacy.nothing_hidden(fresh.get())
    world.store.hide.add("c_hid")
    now[0] += privacy.SUPPRESSION_MAX_AGE_S + 1
    assert not privacy.nothing_hidden(fresh.get())
    assert ask.done


# A follow-up Ask carries the earlier answer into a new run (A12).
def test_a_follow_up_is_given_the_projected_parent_not_the_raw_one_a12(world):
    hold(ask_record())
    r = world.client.post("/api/ask", json={"question": "And what about the week after?", "market": "ZA",
                                            "parent_id": ASK})
    assert r.status_code == 202
    parent = agent_app.ASKS[r.json()["ask_id"]].request["parent"]
    assert leaks(parent, [P_HID1]) == []
    assert [c["id"] for c in parent["answer"]["claims"]] == ["c1", "c4"]
    assert "hid_handle" not in parent["question"]


def test_a_follow_up_with_an_unreadable_list_starts_nothing_a12(world):
    world.store.fail = "list"
    hold(ask_record())
    before = set(agent_app.ASKS)
    r = world.client.post("/api/ask", json={"question": "And what about the week after?", "market": "ZA",
                                            "parent_id": ASK})
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"
    assert set(agent_app.ASKS) == before


# A skin report: the skin masks first and suppression wins over the approved list (A13).
def test_a_suppressed_approved_account_is_not_named_in_a_skin_record_a13(world, monkeypatch):
    CREATORS["c_acme"] = {"creator_id": "c_acme", "platform": "tiktok", "handle": "acme_org"}
    try:
        world.store.hide = {"c_acme"}
        record = ask_record()
        record["skin_id"] = "sk_fixture"
        record["answer"]["evidence"].append(ev("tiktok_acme", "acme_org", "Acme words"))
        record["answer"]["claims"].append({"id": "c5", "text": "Acme posted", "label": "observed", "kind": "observation",
                                           "evidence_ids": ["tiktok_acme"]})
        monkeypatch.setattr(agent_app, "skin_people", lambda sid, rec=None: {"skin_id": sid, "approved": ["tiktok:acme_org"],
                                                                              "allowed": []})
        hold(record)
        body = world.client.get(f"/api/ask/{ASK}").json()
        assert "acme_org" not in body_of(body) and "tiktok_acme" not in body_of(body)
        world.store.fail = "list"
        assert "acme_org" not in body_of(world.client.get(f"/api/ask/{ASK}").json())
    finally:
        CREATORS.pop("c_acme", None)


# Investigations (A20, A21).
def investigation(world, monkeypatch, record):
    from core.api import investigations
    row = investigations.storage_row("i_0123456789ab", 2, "2026-10-07T09:00:00+02:00", "complete", record["question"],
                                     "ZA", {}, {"credits": 0, "model_usd": 0, "minutes": 1}, ASK, "r_x")
    agent_app.INVESTIGATIONS[:] = [row]
    agent_app.SINK.append({"stage": "ask", "record": json.dumps(record)})
    monkeypatch.setattr(agent_app, "investigation_record", lambda ask_id: copy.deepcopy(record))
    return "i_0123456789ab"


def test_an_investigation_read_projects_its_record_a20(world, monkeypatch):
    inv = investigation(world, monkeypatch, ask_record())
    body = world.client.get(f"/api/investigations/{inv}").json()
    assert leaks(body["record"], [P_HID1]) == [] and body["record"]["privacy"]["state"] == "applied"
    assert [c["id"] for c in body["record"]["answer"]["claims"]] == ["c1", "c4"]
    world.store.fail = "list"
    assert world.client.get(f"/api/investigations/{inv}").json()["record"]["privacy"]["state"] == "unavailable"


def test_an_investigation_replay_masks_its_stored_steps_a21(world, monkeypatch):
    inv = investigation(world, monkeypatch, ask_record())
    r = world.client.get(f"/api/investigations/{inv}/events")
    events = sse(r.text)
    assert leaks(r.text) == [] and events[-1][0] == "done"
    assert [d["seq"] for k, d in events if k == "step"] == [1, 2]


# f42-api (A15 to A18, A45).
def remote_agent(ctx_seen, held=None, status=200, body=None, events=None):
    agent = FastAPI()

    @agent.get("/api/ask/{ask_id}")
    async def read(ask_id: str):
        if status != 200:
            return JSONResponse(body or {"error": "iam"}, status_code=status)
        if held is None:
            return JSONResponse({"error": "not_found", "message": "not held"}, status_code=404)
        return JSONResponse(held)

    @agent.get("/api/ask/{ask_id}/events")
    async def stream(ask_id: str):
        if events is None:
            return JSONResponse({"error": "not_found", "message": "not held"}, status_code=404)

        async def gen():
            yield events
        return StreamingResponse(gen(), media_type="text/event-stream")

    return agent


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", PASS)
    for name in ("F42_AUTH_MODE", "IAP_AUDIENCE", "IAP_ALLOWED_EMAILS", "AGENT_URL", "AGENT_AUDIENCE", "MODEL_DAILY_USD",
                 "ASK_PER_IP_DAILY", "LP_ALLOW_OPEN_GATE"):
        monkeypatch.delenv(name, raising=False)
    store = RouteStore(hide={"c_hid"})
    import types as t
    monkeypatch.setattr(core.api, "store", t.SimpleNamespace(get_store=lambda: store), raising=False)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()
    holder = t.SimpleNamespace(store=store, agent=None)

    def serve(app):
        monkeypatch.setattr(core.api, "agent_app", t.SimpleNamespace(app=app), raising=False)
        return TestClient(api_mod.app)

    holder.serve = serve
    return holder


def test_a_stored_record_the_agent_does_not_hold_is_projected_by_the_api_a15(api):
    api.store.records[ASK] = ask_record()
    client = api.serve(remote_agent({}))
    r = client.get(f"/api/ask/{ASK}", headers=GOOD)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == json.loads(json.dumps(privacy.project_record(ask_record(), api.store)))
    assert [c["id"] for c in r.json()["answer"]["claims"]] == ["c1", "c4"]


def test_a_record_an_older_agent_returns_unprojected_is_projected_again_by_the_api_a18(api):
    client = api.serve(remote_agent({}, held=ask_record()))
    body = client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert leaks(body, [P_HID1]) == [] and body["privacy"]["withheld"]["claims"] == 2


def test_the_agents_marker_is_merged_with_the_apis_own_not_trusted_a18(api):
    agent_side = privacy.project_record(ask_record(), api.store)
    client = api.serve(remote_agent({}, held=agent_side))
    body = client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert body["privacy"] == agent_side["privacy"]
    forged = copy.deepcopy(agent_side)
    forged["privacy"] = {"v": 1, "state": "applied", "withheld": "lots"}
    assert "privacy" not in api.serve(remote_agent({}, held=forged)).get(f"/api/ask/{ASK}", headers=GOOD).json() \
        or api.serve(remote_agent({}, held=forged)).get(f"/api/ask/{ASK}", headers=GOOD).json()["privacy"] != forged["privacy"]


def test_a_stored_events_replay_masks_the_question_and_the_steps_a16(api):
    api.store.records[ASK] = ask_record()
    client = api.serve(remote_agent({}))
    r = client.get(f"/api/ask/{ASK}/events", headers={**GOOD, "Last-Event-ID": "0"})
    assert r.status_code == 200 and leaks(r.text) == []
    assert [d["seq"] for k, d in sse(r.text) if k == "step"] == [1, 2]
    resumed = client.get(f"/api/ask/{ASK}/events", headers={**GOOD, "Last-Event-ID": "1"})
    assert [d["seq"] for k, d in sse(resumed.text) if k == "step"] == [2]
    api.store.fail = "list"
    assert leaks(client.get(f"/api/ask/{ASK}/events", headers=GOOD).text) == []


def test_the_apis_relay_passes_the_agents_stream_through_unchanged_a17(api):
    wire = 'id: 1\nevent: step\ndata: {"seq": 1, "text": "Reading 3 posts"}\n\nid: 2\nevent: done\ndata: {"seq": 2, "status": "complete", "url": "/api/ask/a"}\n\n'
    r = api.serve(remote_agent({}, events=wire)).get(f"/api/ask/{ASK}/events", headers=GOOD)
    assert r.text == wire


def test_the_html_export_of_a_hidden_creators_record_is_clean_and_carries_a_notice_a18(api):
    api.store.records[ASK] = ask_record()
    r = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 200 and leaks(r.text, [P_HID1]) == []
    assert privacy.EXPORT_NOTICE in r.text and "Amapiano posts rose" in r.text


def test_an_export_where_every_claim_is_withheld_is_a_partial_page_a18(api):
    api.store.records[ASK] = ask_all_hidden()
    r = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 200 and "Partial answer" in r.text and leaks(r.text, [P_HID1]) == []


def test_an_export_with_an_unreadable_list_is_refused_a18(api):
    api.store.records[ASK] = ask_record()
    api.store.fail = "list"
    r = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"
    assert r.json()["message"] == privacy.PEOPLE_UNAVAILABLE


def test_the_export_of_a_record_the_agent_holds_is_projected_a_second_time_and_keeps_its_notice_a18(api):
    agent_side = privacy.project_record(ask_record(), api.store)
    r = api.serve(remote_agent({}, held=agent_side)).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 200 and privacy.EXPORT_NOTICE in r.text and leaks(r.text, [P_HID1]) == []


def test_an_upstream_refusal_from_cloud_run_reaches_the_browser_as_502_not_401_a45(api):
    for status in (401, 403):
        r = api.serve(remote_agent({}, status=status, body=None)).get(f"/api/ask/{ASK}", headers=GOOD)
        assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"
    own = TestClient(api_mod.app).get(f"/api/ask/{ASK}")
    assert own.status_code == 401 and own.json()["error"] == "unauthorized"


def test_the_apis_projection_reads_the_list_on_every_request_a44(api):
    api.store.records[ASK] = ask_record()
    client = api.serve(remote_agent({}))
    api.store.hide.clear()
    assert "hid_handle" in body_of(client.get(f"/api/ask/{ASK}", headers=GOOD).json())
    api.store.hide.add("c_hid")
    assert "hid_handle" not in body_of(client.get(f"/api/ask/{ASK}", headers=GOOD).json())
    assert api.store.calls.count("suppressed_creators") == 2


def test_nothing_is_written_by_any_reader_a35(api, world):
    api.store.records[ASK] = ask_record()
    api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD)
    assert api.store.writes == [] and api.store.records[ASK] == ask_record()


def test_the_history_list_masks_a_hidden_creators_handle_in_a_question_a19(api):
    from core.api import history
    out = history.build_history_asks(api.store, 20)
    assert "hid_handle" not in body_of(out) and out["asks"][0]["ask_id"] == ASK
    api.store.fail = "list"
    assert "@hid_handle" not in body_of(history.build_history_asks(api.store, 20))
    api.store.fail, api.store.hide = None, set()
    assert "hid_handle" in body_of(history.build_history_asks(api.store, 20))
