"""The hop from f42-agent to f42-api (ruling C1-HOP, deviation D1 from C1 v2 5.2 and 6.3).

The agent runs meta_view on the raw record before its own privacy pass. The wire value crosses the hop, and f42-api
re-checks it with check_wire, which can only lower a state. The privacy projection and the renderers never call a
verifier: they hold the wire value, trust check == verified only, and downgrade anything else. These tests pin each
half, and the one residual the ruling names (a wire-shaped value planted in a runs row that an older agent forwards
raw) as a strict expected failure."""
import copy
import html
import json
import logging
import types

import pytest

import core.api
from core.agent import answer_state
from core.api import agent_app, dossiers, export, privacy, summary_state
from core.api.tests.test_answer_meta_api import (  # noqa: F401  (the fixtures come with them)
    GOOD, KINDS, agent, api, build_body, make, meta_for, with_meta, wire_of,
)
from core.api.tests.test_privacy_ask_routes import ASK, RouteStore, hold, investigation, older_agent, remote_agent, sse
from core.api.tests.test_privacy_projection import ask_record

K6 = "The one-line summary was removed because it used a term or source the trust rules do not allow."
K8 = "The one-line summary was removed because it quoted words that are not in the posts it cites."
NEUTRAL = "The one-line summary is not available for this answer."
PLANT = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
         "summary": {"state": "removed", "removals": [{"stage": "first_check", "cause": "K8"}],
                     "rewrite": "not_attempted"}}
FORBIDDEN = {"check": "unverified", "problem": "forbidden_key"}
SHAPE = {"check": "unverified", "problem": "shape"}
STORED_ONLY = ("digest", "bound", "check_run_id", "ask_id")


def planted():
    """A record whose real stored state is first_check K6 and whose answer_meta is a well formed wire value that fits
    the record (blank, partial, removed) and names K8."""
    record = with_meta("removed")
    assert answer_state.verify_stored(record) is None
    assert record["answer_meta"]["summary"]["removals"] == [{"stage": "first_check", "cause": "K6"}]
    return {**record, "answer_meta": copy.deepcopy(PLANT)}


def spliced():
    """A stored shape built for another ask, with a digest that was recomputed."""
    record = with_meta("removed")
    record["answer_meta"] = answer_state.build(
        ask_id="a_20261007_other", check_run_id=record["run"]["run_id"], answer=record["answer"],
        execution_state="completed", stop_reason=None, summary_state="removed", removals=[("first_check", "K6")],
        rewrite="not_attempted")
    return record


def clean(text):
    return html.unescape(text)


def no_stored_keys(value):
    text = json.dumps(value)
    return not any(f'"{key}"' in text for key in STORED_ONLY)


# T1 and T2: every path with the new agent refuses a plant and a splice.
def put_in_agent(record):
    hold(record)


def put_in_runs(api, record):
    agent_app.ASKS.clear()
    api.store.records[ASK] = copy.deepcopy(record)


def read_live(api, record, monkeypatch):
    put_in_agent(record)
    return api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"]


def read_stored(api, record, monkeypatch):
    put_in_runs(api, record)
    return api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"]


def read_wait_post(api, record, monkeypatch):
    def raw_keep(ask, update, meta):  # a value that reached the record some other way than the producer's
        update["answer_meta"] = copy.deepcopy(record["answer_meta"])

    monkeypatch.setattr(agent_app, "keep_state", raw_keep)
    monkeypatch.setattr(agent_app, "resolve_run_ask",
                        lambda: lambda req, emit, stop: {"answer": record["answer"], "run": record["run"]})
    got = api.client.post("/api/ask", headers=GOOD, json={"question": "What is amapiano doing?", "market": "ZA", "wait": True}).json()
    return got["answer_meta"]


def read_investigation(api, record, monkeypatch):
    inv = investigation(types.SimpleNamespace(store=api.store), monkeypatch, record)
    return api.client.get(f"/api/investigations/{inv}", headers=GOOD).json()["record"]["answer_meta"]


def page_live(api, record, monkeypatch):
    put_in_agent(record)
    return api.client.get(f"/api/ask/{ASK}/export", headers=GOOD).text


def page_stored(api, record, monkeypatch):
    put_in_runs(api, record)
    return api.client.get(f"/api/ask/{ASK}/export", headers=GOOD).text


def dossier_page(api, record, monkeypatch):
    put_in_agent(record)
    made = api.client.post("/api/dossiers", headers=GOOD, json={"from": {"ask_id": ASK}}).json()
    assert made["source_answer_meta"] in (FORBIDDEN, {"check": "unverified", "problem": "ids"}), made["source_answer_meta"]
    did = made["dossier_id"]
    edited = api.client.put(f"/api/dossiers/{did}", headers=GOOD, json={"title": "t", "from_version": 1}).json()
    assert edited["source_answer_meta"] == made["source_answer_meta"]
    for claim in ("c2", "c4"):
        assert api.client.post(f"/api/dossiers/{did}/ticks", headers=GOOD,
                               json={"claim_id": claim, "ticked": True}).status_code == 201
    frozen = api.client.post(f"/api/dossiers/{did}/freeze", headers=GOOD, json={"from_version": 2}).json()
    assert frozen["source_answer_meta"] == made["source_answer_meta"] and frozen["summary_state"] == "legacy_unknown"
    return api.client.get(f"/api/dossiers/{did}/versions/3/export?format=html", headers=GOOD).text


META_PATHS = [read_live, read_stored, read_wait_post, read_investigation]
PAGE_PATHS = [page_live, page_stored, dossier_page]


@pytest.mark.parametrize("path", META_PATHS, ids=lambda p: p.__name__)
def test_a_well_formed_wire_value_planted_in_a_record_is_refused_on_every_path_t1(api, monkeypatch, path):
    meta = path(api, planted(), monkeypatch)
    assert meta == FORBIDDEN and no_stored_keys(meta)


@pytest.mark.parametrize("path", PAGE_PATHS, ids=lambda p: p.__name__)
def test_a_planted_value_never_reaches_a_page_t1(api, monkeypatch, path):
    page = clean(path(api, planted(), monkeypatch))
    assert K8 not in page and K6 not in page and NEUTRAL in page
    assert "digest" not in page and "check_run_id" not in page


def test_a_planted_value_leaves_no_state_in_the_stored_replay_t1(api):
    put_in_runs(api, planted())
    text = api.client.get(f"/api/ask/{ASK}/events", headers=GOOD).text
    assert "answer_meta" not in text and set(sse(text)[-1][1]) == {"seq", "status", "url"}


def test_history_reopens_a_planted_record_through_the_stored_read_t1(api):
    put_in_runs(api, planted())
    assert api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"] == FORBIDDEN


@pytest.mark.parametrize("path", META_PATHS, ids=lambda p: p.__name__)
def test_a_state_built_for_another_ask_is_refused_on_every_path_t2(api, monkeypatch, path):
    meta = path(api, spliced(), monkeypatch)
    assert meta == {"check": "unverified", "problem": "ids"} and no_stored_keys(meta)


def test_a_state_built_for_another_ask_reaches_no_page_t2(api, monkeypatch):
    for path in PAGE_PATHS:
        page = clean(path(api, spliced(), monkeypatch))
        assert K6 not in page and NEUTRAL in page, path.__name__


# T3: the privacy projection holds answer_meta out of the pass and never judges it.
def boom(*_args, **_kwargs):
    raise AssertionError("the projection called a verifier")


def block_verifiers(monkeypatch):
    """Called after the test has built its records: from here on any verifier call fails the test."""
    for name in ("meta_view", "verify_stored", "check_wire"):
        monkeypatch.setattr(answer_state, name, boom)


@pytest.mark.parametrize("lists", ["hidden", "unavailable", "nothing"])
def test_the_projection_hands_on_a_wire_value_byte_for_byte_and_calls_no_verifier_t3(lists, monkeypatch):
    record = ask_record()
    record["run"] = {"run_id": "r_x"}
    wire = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
            "summary": {"state": "shown", "removals": [], "rewrite": "not_attempted"}}
    record["answer_meta"] = copy.deepcopy(wire)
    store = RouteStore(hide={"c_hid"} if lists != "nothing" else set(), fail="list" if lists == "unavailable" else None)
    block_verifiers(monkeypatch)
    shown = privacy.project_record(record, store, privacy.read_hidden(store))
    assert shown["answer_meta"] == wire
    assert json.dumps(shown["answer_meta"], sort_keys=True) == json.dumps(wire, sort_keys=True)
    assert ("privacy" in shown) is (lists != "nothing")


def test_the_projection_replaces_a_stored_shape_with_unverified_and_logs_it_t3(monkeypatch, caplog):
    record = with_meta("shown")
    store = RouteStore(hide={"c_hid"})
    block_verifiers(monkeypatch)
    with caplog.at_level(logging.ERROR):
        shown = privacy.project_record(record, store, privacy.read_hidden(store))
    assert shown["answer_meta"] == SHAPE and "unprojected" in caplog.text
    assert no_stored_keys(shown["answer_meta"])


def test_the_projection_does_not_judge_a_wire_value_so_the_routes_must_t3(monkeypatch):
    record = planted()  # a wire shape is not a boundary here: with_wire and the agent are, and T1 pins them
    store = RouteStore(hide=set())
    block_verifiers(monkeypatch)
    assert privacy.project_record(record, store, privacy.read_hidden(store))["answer_meta"] == PLANT


def test_a_running_record_and_one_without_the_key_pass_untouched_t3(monkeypatch, caplog):
    store = RouteStore(hide={"c_hid"})
    running = {**ask_record(), "status": "running", "answer": None, "answer_meta": None}
    block_verifiers(monkeypatch)
    with caplog.at_level(logging.ERROR):
        assert privacy.project_record(running, store, privacy.read_hidden(store)).get("answer_meta") is None
        assert "answer_meta" not in privacy.project_record(ask_record(), store, privacy.read_hidden(store))
    assert caplog.text == ""


# T4: the export reads the wire value it is given.
def test_the_export_prints_a_verified_wire_value_with_no_verifier_t4(monkeypatch):
    record = with_meta("removed")
    sent = {**record, "answer_meta": {"check": "verified", "v": 1,
                                      "execution": {"state": "completed", "stop_reason": None},
                                      "summary": {"state": "removed", "removals": [{"stage": "first_check", "cause": "K6"}],
                                                  "rewrite": "not_attempted"}}}
    block_verifiers(monkeypatch)
    assert K6 in clean(export.render_answer_html(sent))


@pytest.mark.parametrize("meta", [SHAPE, FORBIDDEN, {"check": "legacy_unknown"}, None, "x"],
                         ids=["unverified", "forbidden", "legacy", "none", "string"])
def test_the_export_prints_the_neutral_sentence_for_anything_not_verified_t4(meta, monkeypatch):
    record = {**with_meta("removed"), "answer_meta": meta}
    block_verifiers(monkeypatch)
    page = clean(export.render_answer_html(record))
    assert NEUTRAL in page and K6 not in page


def test_the_export_does_not_believe_a_stored_shape_t4(monkeypatch):
    record = with_meta("removed")
    block_verifiers(monkeypatch)
    page = clean(export.render_answer_html(record))
    assert NEUTRAL in page and K6 not in page and "sha256" not in page


# T5: an a80 agent forwards what it holds, and the API judges it with the full check where it can.
def inv_payload(record):
    return {"investigation_id": "i_0123456789ab", "status": "complete", "question": record["question"], "plan": {},
            "record": record}


def forwarded(api, monkeypatch, record):
    app = older_agent({"/api/investigations/i_0123456789ab": (200, inv_payload(record))})
    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=app), raising=False)
    return api.client.get("/api/investigations/i_0123456789ab", headers=GOOD).json()["record"]["answer_meta"]


def test_an_a80_agents_live_read_of_a_record_with_no_key_is_legacy_t5(api, monkeypatch):
    record = ask_record()
    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=remote_agent({}, held=record)), raising=False)
    assert api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"] == {"check": "legacy_unknown"}


def test_an_a80_agent_forwarding_stored_rows_is_judged_with_the_full_check_t5(api, monkeypatch):
    good = with_meta("removed")
    stale = with_meta("removed")
    stale["answer_meta"]["summary"]["removals"] = [{"stage": "first_check", "cause": "K8"}]  # edited, digest kept
    legacy = ask_record()
    legacy["answer"].update(status="partial", short_answer="")
    got = [forwarded(api, monkeypatch, r) for r in (good, stale, spliced(), legacy)]
    assert got[0]["check"] == "verified" and set(got[0]) == {"check", "v", "execution", "summary"}
    assert got[1] == {"check": "unverified", "problem": "digest"}
    assert got[2] == {"check": "unverified", "problem": "ids"}
    assert got[3] == {"check": "legacy_unknown"}


@pytest.mark.xfail(strict=True, reason="deviation D1: believed on pairing over the hop; closed only by Q7")
def test_an_a80_agent_forwarding_a_planted_wire_row_is_refused_t5(api, monkeypatch):
    assert forwarded(api, monkeypatch, planted()) == FORBIDDEN


# T6: the agent's wire value survives f42-api's second pass after the agent withheld content.
@pytest.mark.parametrize("lists", ["hidden", "unavailable"])
@pytest.mark.parametrize("kind", KINDS)
def test_the_agents_wire_value_stays_verified_through_the_second_pass_t6(api, monkeypatch, kind, lists):
    api.store.hide, api.store.fail = {"c_hid"}, "list" if lists == "unavailable" else None
    record = with_meta(kind)
    sent = agent_app.readable(record, privacy.read_hidden(api.store))  # the agent: meta_view first, then its own pass
    assert sent["answer_meta"]["check"] == "verified", sent["answer_meta"]
    monkeypatch.setattr(core.api, "agent_app",
                        types.SimpleNamespace(app=remote_agent({}, held=json.loads(json.dumps(sent)))), raising=False)
    body = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert body["answer_meta"] == sent["answer_meta"], (kind, lists, body["answer_meta"])


# T7: with_wire runs first on every route, so the projection never meets a stored shape.
@pytest.fixture
def spy(monkeypatch):
    seen = []
    real = privacy.project_record

    def watching(record, *args, **kwargs):
        if isinstance(record, dict) and record.get("answer_meta") is not None:
            seen.append(record["answer_meta"])
            assert summary_state.is_wire(record["answer_meta"]), "a stored shape reached the projection"
        return real(record, *args, **kwargs)

    monkeypatch.setattr(privacy, "project_record", watching)
    return seen


@pytest.mark.parametrize("path", [read_live, read_stored, read_wait_post, read_investigation, page_live, page_stored],
                         ids=lambda p: p.__name__)
def test_no_route_hands_the_projection_a_stored_shape_t7(api, monkeypatch, spy, path):
    got = path(api, with_meta("removed"), monkeypatch)
    assert got  # the route answered
    assert spy, "the route never reached the projection"


def test_no_older_agent_route_hands_the_projection_a_stored_shape_t7(api, monkeypatch, spy):
    record = with_meta("shown")
    inv = {"investigation_id": "i_0123456789ab", "status": "complete", "question": record["question"], "plan": {},
           "record": record}
    payloads = {f"/api/ask/{ASK}": (200, record), "/api/ask": (200, record),
                "/api/investigations/i_0123456789ab": (200, inv)}
    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=older_agent(payloads)), raising=False)
    api.client.get(f"/api/ask/{ASK}", headers=GOOD)
    api.client.post("/api/ask", headers=GOOD, json={"question": "q", "wait": True})
    api.client.get("/api/investigations/i_0123456789ab", headers=GOOD)
    assert len(spy) == 3 and all(m["check"] == "verified" for m in spy)
