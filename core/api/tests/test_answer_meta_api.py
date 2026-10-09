"""The typed summary state on the API side (C1 v2: sections 3 to 6, tests P-01 to P-19).

Lane 1's core/agent/answer_state.py is the producer and the verifier; these tests run it for real, with no double,
through the real execute, the real agent routes and f42-api's own routes. Only code sets the state: the model's
metadata is rejected, a forged value reads as unverified, and a record without one reads as legacy."""
import copy
import html
import json
import logging
import time
import types

import pytest
from fastapi.testclient import TestClient

import core.api
from core.agent import answer_state
from core.api import agent_app, auth, privacy
from core.api import app as api_mod
from core.api.tests.test_privacy_ask_routes import ASK, RouteStore, hold, sse
from core.api.tests.test_privacy_projection import P_HID1, ask_record, body_of, leaks

GOOD = {"X-Passcode": "s3cret-passcode"}
FIXED_TEXT = "Stopped on request before any claim passed the checks."


def make(kind):
    """(record, build kwargs) for one producer state: a terminal record whose answer fits the state."""
    record = ask_record()
    record["run"] = {"run_id": "r_20261007_aaaa0001", "posts": 3}
    answer = record["answer"]
    kw = dict(execution_state="completed", stop_reason=None, summary_state="shown", removals=[], rewrite="not_attempted")
    if kind == "shown_rewritten":
        kw.update(summary_state="shown_rewritten", removals=[("support_check", "claim_cut")], rewrite="kept")
    elif kind in ("fixed_text", "stopped_on_request", "stopped_on_budget_full", "stopped_on_budget_unverified",
                  "stopped_on_budget_price", "refused_budget_spent"):
        answer.update(status="insufficient_evidence", claims=[], so_what=[], watch_next=[], evidence=[],
                      short_answer=FIXED_TEXT)
        kw.update(summary_state="fixed_text")
        if kind == "stopped_on_request":
            record["status"] = "stopped"
            kw.update(execution_state="stopped_on_request")
        elif kind.startswith("stopped_on_budget"):
            kw.update(execution_state="stopped_on_budget",
                      stop_reason={"full": "budget_full", "unverified": "model_call_unverified",
                                   "price": "price_unreadable"}[kind.rsplit("_", 1)[1]])
        elif kind == "refused_budget_spent":
            kw.update(execution_state="refused_budget_spent")
    elif kind == "removed":
        answer.update(status="partial", short_answer="")
        kw.update(summary_state="removed", removals=[("first_check", "K6")])
    elif kind == "blank_unexplained":
        answer.update(short_answer="  ")
        kw.update(summary_state="blank_unexplained")
    return record, kw


KINDS = ["shown", "shown_rewritten", "fixed_text", "stopped_on_request", "stopped_on_budget_full",
         "stopped_on_budget_unverified", "stopped_on_budget_price", "refused_budget_spent", "removed",
         "blank_unexplained"]


def meta_for(record, kw, **over):
    return answer_state.build(ask_id=record["ask_id"], check_run_id=record["run"]["run_id"], answer=record["answer"],
                              **{**kw, **over})


def with_meta(kind):
    record, kw = make(kind)
    record["answer_meta"] = meta_for(record, kw)
    return record


def test_every_fixture_state_is_one_the_producer_would_store():
    for kind in KINDS:
        assert answer_state.verify_stored(with_meta(kind)) is None, kind


# execute stores what the producer built, after judging it against the record as it will be stored (P-01 to P-03).
def run_execute(returned=None, raises=None, stop=False, request=None):
    request = request or {"ask_id": ASK, "question": "What is amapiano doing?", "market": "ZA"}
    ask = agent_app.Ask(request)
    ask.stop = stop

    def run_ask(req, emit, should_stop):
        if raises:
            raise raises
        return copy.deepcopy(returned)

    agent_app.SINK.clear()
    agent_app.execute(ask, run_ask)
    return ask


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.delenv("F42_DATA", raising=False)
    store = RouteStore(hide=set())
    monkeypatch.setattr("core.api.store.get_store", lambda: store)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield types.SimpleNamespace(store=store, client=TestClient(agent_app.app))
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.ASKS.clear()
    agent_app.SINK.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_execute_stores_the_state_beside_the_answer_and_not_inside_it_p01(agent, kind):
    record, kw = make(kind)
    returned = {"answer": record["answer"], "run": record["run"], "answer_meta": meta_for(record, kw)}
    ask = run_execute(returned, stop=record["status"] == "stopped")
    held = ask.snapshot()
    assert held["answer_meta"] == returned["answer_meta"] and held["status"] == record["status"]
    row = agent_app.SINK[-1]
    assert json.loads(row["record"])["answer_meta"] == returned["answer_meta"]
    assert "answer_meta" not in json.loads(row["answer"])
    assert answer_state.verify_stored(held) is None


def test_a_state_that_does_not_fit_the_record_is_not_stored_and_the_answer_is_p02(agent, caplog):
    record, kw = make("shown")
    bad = meta_for(record, kw, summary_state="removed", removals=[("first_check", "K6")])  # but the summary is shown
    with caplog.at_level(logging.ERROR, logger="f42-agent"):
        ask = run_execute({"answer": record["answer"], "run": record["run"], "answer_meta": bad})
    held = ask.snapshot()
    assert "answer_meta" not in held and held["answer"] == record["answer"] and held["status"] == "complete"
    assert "answer_meta rejected" in caplog.text


def test_a_state_the_model_could_have_written_is_not_stored_p02(agent):
    record, kw = make("shown")
    forged = meta_for(record, kw)
    forged["summary"]["state"] = "removed"  # edited after the digest was made
    forged2 = dict(meta_for(record, kw), check="verified")  # carries the wire key
    for meta in (forged, forged2, "verified", {"summary": "shown"}, ["shown"], 7):
        ask = run_execute({"answer": record["answer"], "run": record["run"], "answer_meta": meta})
        assert "answer_meta" not in ask.snapshot(), meta


def test_a_fault_in_the_metadata_code_never_fails_the_ask_p02b(agent, monkeypatch, caplog):
    record, kw = make("shown")

    def boom(_record):
        raise RuntimeError("verifier broke")

    monkeypatch.setattr(answer_state, "verify_stored", boom)
    with caplog.at_level(logging.ERROR, logger="f42-agent"):
        ask = run_execute({"answer": record["answer"], "run": record["run"], "answer_meta": meta_for(record, kw)})
    held = ask.snapshot()
    assert held["status"] == "complete" and held["answer"] == record["answer"] and "answer_meta" not in held
    assert ask.finished.is_set() and agent_app.SINK
    assert "answer_meta" in caplog.text


def test_a_run_that_returns_no_state_stays_without_one_p02(agent):
    record, _ = make("shown")
    assert "answer_meta" not in run_execute({"answer": record["answer"], "run": record["run"]}).snapshot()


@pytest.mark.parametrize("with_run", [True, False])
def test_a_run_that_raised_stores_the_failed_state_p03(agent, with_run):
    exc = RuntimeError("the agent broke")
    if with_run:
        exc.run = {"run_id": "r_20261007_failed1", "model_usd": 0.5}
    ask = run_execute(raises=exc)
    held = ask.snapshot()
    assert held["status"] == "failed" and held["answer"] is None
    meta = held["answer_meta"]
    assert meta["execution"]["state"] == "failed" and meta["summary"]["state"] == "no_answer"
    assert meta["check_run_id"] == ("r_20261007_failed1" if with_run else None)  # the partial run's id, else none
    assert answer_state.verify_stored(held) is None
    assert json.loads(agent_app.SINK[-1]["record"])["answer_meta"] == meta


def test_a_snapshot_never_shows_a_finished_status_without_the_state_that_belongs_to_it_p04(agent):
    record, kw = make("shown")
    seen = []
    ask = agent_app.Ask({"ask_id": ASK, "question": "q", "market": "ZA"})
    original = agent_app.sink

    def slow_sink(held):
        seen.append(held.snapshot())  # the sink runs after the record flipped: both are there together
        return original(held)

    agent_app.sink = slow_sink
    try:
        agent_app.execute(ask, lambda req, emit, stop: {"answer": record["answer"], "run": record["run"],
                                                         "answer_meta": meta_for(record, kw)})
    finally:
        agent_app.sink = original
    assert seen[0]["status"] == "complete" and seen[0]["answer"] and seen[0]["answer_meta"]


def test_a_client_cannot_name_the_state_p05(agent):
    r = agent.client.post("/api/ask", json={"question": "What is amapiano doing?", "market": "ZA",
                                            "answer_meta": {"check": "verified"}, "answer_state": "x",
                                            "summary_state": "shown"})
    assert r.status_code == 202
    ask = agent_app.ASKS[r.json()["ask_id"]]
    ask.finished.wait(10)
    held = ask.snapshot()
    assert "answer_state" not in held and "summary_state" not in held
    assert held.get("answer_meta") != {"check": "verified"} and "check" not in (held.get("answer_meta") or {})


def test_the_fixture_agent_returns_a_state_the_verifier_accepts_p19(agent):
    r = agent.client.post("/api/ask", json={"question": "What is behind #fixture in South Africa this week?",
                                            "market": "ZA", "wait": True})
    record = r.json()
    assert record["answer_meta"]["check"] == "verified" and answer_state.check_wire(record) is None
    stored = json.loads(agent_app.SINK[-1]["record"])
    assert answer_state.verify_stored(stored) is None
    assert stored["answer_meta"]["check_run_id"] == record["run"]["run_id"]


# What the agent route and f42-api send the browser: the wire value, never the stored one (P-06, P-10).
def wire_of(record):
    return answer_state.meta_view(record)


@pytest.mark.parametrize("kind", KINDS)
def test_the_agent_route_sends_the_wire_value_p06(agent, kind):
    record = with_meta(kind)
    hold(record)
    body = agent.client.get(f"/api/ask/{ASK}").json()
    assert body["answer_meta"] == wire_of(record) and body["answer_meta"]["check"] == "verified"
    assert set(body["answer_meta"]) == {"check", "v", "execution", "summary"}
    assert answer_state.check_wire(body) is None


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret-passcode")
    for name in ("F42_AUTH_MODE", "IAP_AUDIENCE", "IAP_ALLOWED_EMAILS", "AGENT_URL", "AGENT_AUDIENCE", "MODEL_DAILY_USD",
                 "ASK_PER_IP_DAILY", "LP_ALLOW_OPEN_GATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.delenv("F42_DATA", raising=False)
    store = RouteStore(hide=set())
    monkeypatch.setattr("core.api.store.get_store", lambda: store)
    monkeypatch.setattr(core.api, "store", types.SimpleNamespace(get_store=lambda: store), raising=False)
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)  # the real f42-agent, in process
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    auth.daily_questions.counts.clear()
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield types.SimpleNamespace(store=store, client=TestClient(api_mod.app))
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.ASKS.clear()
    agent_app.SINK.clear()


@pytest.mark.parametrize("kind", KINDS)
def test_f42_api_returns_the_wire_value_live_and_stored_the_same_p06(api, kind):
    record = with_meta(kind)
    hold(record)
    live = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    agent_app.ASKS.clear()
    api.store.records[ASK] = copy.deepcopy(record)
    stored = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert live["answer_meta"] == stored["answer_meta"] == wire_of(record)
    assert answer_state.check_wire(live) is None and answer_state.check_wire(stored) is None
    for key in ("digest", "bound", "ask_id", "check_run_id"):
        assert key not in stored["answer_meta"]


def test_a_real_ask_through_the_real_agent_and_the_api_passes_the_wire_check_p07(api):
    r = api.client.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                        "market": "ZA", "wait": True})
    assert r.status_code == 200
    posted = r.json()
    assert posted["answer_meta"]["check"] == "verified" and answer_state.check_wire(posted) is None
    got = api.client.get(f"/api/ask/{posted['ask_id']}", headers=GOOD).json()
    assert got["answer_meta"] == posted["answer_meta"]


def test_a_running_record_has_no_state_and_a_finished_one_has_the_same_state_on_every_poll_p10(api):
    ask = agent_app.Ask({"ask_id": ASK, "question": "q", "market": "ZA"})
    agent_app.ASKS[ASK] = ask
    first = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert first["status"] == "running" and first["answer"] is None and first["answer_meta"] is None
    record, kw = make("shown")
    agent_app.execute(ask, lambda req, emit, stop: {"answer": record["answer"], "run": record["run"],
                                                    "answer_meta": meta_for(record, kw)})
    polls = [api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"] for _ in range(3)]
    assert polls[0] == polls[1] == polls[2] and polls[0]["check"] == "verified"
    agent_app.ASKS.clear()  # past the hour: the runs row the sink wrote is what a reader gets
    api.store.records[ASK] = json.loads(agent_app.SINK[-1]["record"])
    assert api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"] == polls[0]


# Forged, missing and legacy values (P-11, N series).
def test_a_record_with_no_state_reads_as_legacy_p11(api):
    record = ask_record()
    record["run"] = {"run_id": "r_x"}
    api.store.records[ASK] = record
    body = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert body["answer_meta"] == {"check": "legacy_unknown"} and "answer" in body


def recomputed(meta):
    meta["digest"] = answer_state.digest_of(meta)


@pytest.mark.parametrize("edit, problem, redo", [
    (lambda m: m["summary"].update(state="removed"), "digest", False),
    (lambda m: m.update(digest="sha256:" + "0" * 64), "digest", False),
    (lambda m: m.update(v=2), "version", False),
    (lambda m: m.update(extra=1), "shape", False),
    (lambda m: m.update(check="verified"), "forbidden_key", False),
    (lambda m: m.update(ask_id="a_other"), "digest", False),
    (lambda m: m.update(ask_id="a_other"), "ids", True),
    (lambda m: m.update(check_run_id="r_other"), "ids", True),
    (lambda m: m["bound"].update(claims=9), "bound", True),
    (lambda m: m["summary"].update(state="blank_unexplained"), "pairing", True),
    (lambda m: m["summary"].update(rewrite="kept"), "reasons", True),
])
def test_a_forged_stored_state_reads_as_unverified_with_the_step_that_caught_it_p06(api, edit, problem, redo):
    record = with_meta("shown")
    edit(record["answer_meta"])
    if redo:
        recomputed(record["answer_meta"])  # a competent forgery: the digest is right, a later step must still refuse it
    api.store.records[ASK] = record
    wire = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"]
    assert wire == {"check": "unverified", "problem": problem}


def test_a_state_that_a_recomputed_digest_cannot_save_is_unverified_by_what_it_is_bound_to_p06(api):
    record = with_meta("shown")
    record["answer"]["short_answer"] = ""  # the summary is gone, the state still says shown
    api.store.records[ASK] = record
    assert api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"] == {"check": "unverified",
                                                                                      "problem": "bound"}


def test_a_wire_shaped_value_planted_in_a_stored_record_is_not_believed_p06(api):
    record = ask_record()
    record["run"] = {"run_id": "r_x"}
    record["answer_meta"] = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
                             "summary": {"state": "shown", "removals": [], "rewrite": "not_attempted"}}
    api.store.records[ASK] = record
    assert api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()["answer_meta"]["check"] == "unverified"


def test_a_value_the_agent_sends_that_does_not_fit_the_record_is_unverified_at_the_api():
    from core.api import summary_state
    record = with_meta("shown")
    wire = wire_of(record)
    wire["summary"]["state"] = "removed"  # a wire value edited on its way
    sent = {**record, "answer_meta": wire}
    assert summary_state.with_wire(sent, from_agent=True)["answer_meta"]["check"] == "unverified"
    assert summary_state.with_wire({**record, "answer_meta": wire_of(record)}, from_agent=True)["answer_meta"]["check"] == "verified"


# Privacy comes after the state (C1 condition 1): withholding claims does not make the state unverified.
def test_a_record_with_withheld_claims_still_carries_a_verified_state_p06(api):
    api.store.hide = {"c_hid"}
    record = with_meta("shown")
    hold(record)
    live = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert live["privacy"]["withheld"]["claims"] == 2 and live["answer_meta"]["check"] == "verified"
    agent_app.ASKS.clear()
    api.store.records[ASK] = copy.deepcopy(record)
    stored = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert stored["answer_meta"] == live["answer_meta"] and stored["privacy"] == live["privacy"]


def test_the_state_is_structural_to_a_skin_mask_and_survives_it_p12():
    from core.api import skins
    assert "answer_meta" in skins.STRUCTURAL
    record = with_meta("removed")
    record["skin_id"] = "sk_fixture"
    record["answer"]["evidence"].append({"id": "tiktok_1", "platform": "tiktok", "handle": "removed", "url": "u",
                                         "posted_at": "2026-10-05T09:00:00+02:00", "market": "ZA", "text": "x"})
    masked = skins.mask_people({**record, "answer_meta": wire_of(record)}, [], [])
    assert masked["answer_meta"] == wire_of(record)


# The stream and the polls never carry it (P-09); history is unchanged (P-16).
def test_no_event_and_no_replayed_frame_carries_the_state_p09(agent, api):
    record = with_meta("shown")
    hold(record, [("step", {"text": "Reading 3 posts"}),
                  ("evidence", {"evidence": record["answer"]["evidence"][0]})])
    text = agent.client.get(f"/api/ask/{ASK}/events").text
    assert "answer_meta" not in text and set(sse(text)[-1][1]) == {"seq", "status", "url"}
    agent_app.ASKS.clear()
    api.store.records[ASK] = copy.deepcopy(record)
    replay = api.client.get(f"/api/ask/{ASK}/events", headers=GOOD).text
    assert "answer_meta" not in replay and set(sse(replay)[-1][1]) == {"seq", "status", "url"}


def test_the_history_list_keeps_its_keys_p16(api):
    from core.api import history
    row = history.build_history_asks(api.store, 20)["asks"][0]
    assert set(row) == {"ask_id", "question", "at", "status", "answer_status", "market"}


# An investigation's record (P-08).
def test_an_investigation_page_carries_the_wire_value_p08(agent, monkeypatch):
    from core.api.tests.test_privacy_ask_routes import investigation
    record = with_meta("removed")
    inv = investigation(types.SimpleNamespace(store=agent.store), monkeypatch, record)
    body = agent.client.get(f"/api/investigations/{inv}").json()
    assert body["record"]["answer_meta"] == wire_of(record)
    assert answer_state.check_wire(body["record"]) is None


# The export (P-13) and the dossier (P-14, P-17) read the state themselves.
import re

from core.api import dossiers, export

SENTENCES = {  # the proposed copy of C1 2.6, written here as literals
    "removed": "The one-line summary was removed because it used a term or source the trust rules do not allow.",
    "blank_unexplained": "No one-line summary was written for this answer.",
    "neutral": "The one-line summary is not available for this answer.",
}


def short_answer_section(page):
    return re.search(r"<h2>Short answer</h2>(.*?)</section>", page, re.S).group(1)


def test_a_removed_summary_is_explained_in_the_export_from_the_state_not_from_gap_text_p13():
    page = export.render_answer_html(with_meta("removed"))
    text = short_answer_section(page)
    assert SENTENCES["removed"] in text and "did not pass the checks" not in text
    assert "4 checked findings" in text
    assert not re.search(r"\bK(10|[1-9])\b", page)


def test_a_blank_summary_with_no_reason_says_so_p13():
    assert SENTENCES["blank_unexplained"] in short_answer_section(export.render_answer_html(with_meta("blank_unexplained")))


@pytest.mark.parametrize("breaker", ["legacy", "forged", "wire_forged"])
def test_a_blank_summary_with_no_verified_state_gets_the_neutral_sentence_p13(breaker):
    record = with_meta("removed")
    if breaker == "legacy":
        record.pop("answer_meta")
    elif breaker == "forged":
        record["answer_meta"]["summary"]["state"] = "blank_unexplained"
    else:
        record["answer_meta"] = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
                                 "summary": {"state": "blank_unexplained", "removals": [], "rewrite": "not_attempted"}}
        record["answer"]["status"] = "insufficient_evidence"  # it does not fit this record
    text = short_answer_section(export.render_answer_html(record))
    assert SENTENCES["neutral"] in text and SENTENCES["blank_unexplained"] not in text
    assert "did not pass the checks" not in text


def test_the_export_of_the_wire_value_reads_the_same_as_the_export_of_the_raw_record_p13():
    for kind in KINDS:
        record = with_meta(kind)
        sent = {**record, "answer_meta": wire_of(record)}
        assert export.render_answer_html(sent) == export.render_answer_html(record), kind


@pytest.mark.parametrize("kind, words", [
    ("stopped_on_budget_full", "Stopped at this question's model budget, not for lack of evidence"),
    ("stopped_on_budget_unverified", "Stopped because a model call failed or did not report what it cost, not for lack of evidence"),
    ("stopped_on_budget_price", "Stopped because the cost of a model call could not be worked out, not for lack of evidence"),
    ("stopped_on_request", "Stopped before an answer was written"),
    ("refused_budget_spent", "Not researched: the model budget for today is spent"),
])
def test_how_the_run_ended_is_said_from_the_verified_state_p13(kind, words):
    page = export.render_answer_html(with_meta(kind))
    assert words in page.replace("&#x27;", "'")


def test_a_stop_that_came_after_the_answer_was_final_does_not_say_stopped_early_p13():
    record = with_meta("shown")
    record["status"] = "stopped"
    record["answer_meta"] = meta_for(record, make("shown")[1])
    page = export.render_answer_html(record)
    assert "Stopped before the end" not in page
    legacy = {k: v for k, v in record.items() if k != "answer_meta"}
    assert "Stopped before the end" in export.render_answer_html(legacy)


def test_a_rewritten_summary_says_so_under_the_text_p13():
    page = export.render_answer_html(with_meta("shown_rewritten"))
    assert "This summary was rewritten once from the findings that passed." in short_answer_section(page)


def test_the_export_and_the_dossier_read_the_state_through_meta_view_themselves_p17(monkeypatch):
    seen = []
    real = answer_state.meta_view
    monkeypatch.setattr(answer_state, "meta_view", lambda record: seen.append(record) or real(record))
    record = with_meta("shown")
    export.render_answer_html(record)
    dossiers.build(record, {"keep": ["c1"], "title": "t", "notes": {}}, dossier_id="d_1", version=1,
                   created_at="2026-10-07T10:00:00+02:00", source={"ask_id": ASK})
    assert len(seen) == 2 and all(s is record for s in seen)


def build_body(record, keep):
    return dossiers.build(record, {"keep": keep, "title": "t", "notes": {}}, dossier_id="d_1", version=1,
                          created_at="2026-10-07T10:00:00+02:00", source={"ask_id": ASK})


def test_a_new_dossier_body_carries_its_version_and_the_source_state_p14():
    record = with_meta("shown")
    body = build_body(record, ["c1", "c2", "c3", "c4"])
    assert body["body_v"] == 2 and body["source_answer_meta"] == wire_of(record)
    assert dossiers.shown(body)["summary_state"] == "shown"


def test_the_view_says_the_summary_was_left_out_when_not_every_claim_is_kept_p14():
    body = build_body(with_meta("shown"), ["c1"])
    assert dossiers.shown(body)["summary_state"] == "omitted_by_selection" and body["summary"] is None


def test_a_removed_summary_reads_as_removed_and_a_forged_source_state_builds_with_the_summary_withheld_p14_p17():
    record = with_meta("removed")
    body = build_body(record, ["c1", "c2", "c3", "c4"])
    assert dossiers.shown(body)["summary_state"] == "removed"
    forged = copy.deepcopy(with_meta("shown"))
    forged["answer_meta"]["summary"]["state"] = "removed"
    built = build_body(forged, ["c1", "c2", "c3", "c4"])
    assert built["source_answer_meta"]["check"] == "unverified" and built["summary"] is None
    assert [c["claim_id"] for c in built["claims"] if c["kept"]] == ["c1", "c2", "c3", "c4"]
    assert dossiers.shown(built)["summary_state"] == "legacy_unknown"


def test_a_body_with_no_version_or_a_state_that_disagrees_with_its_summary_reads_as_legacy_p14():
    body = build_body(with_meta("shown"), ["c1", "c2", "c3", "c4"])
    old = {k: v for k, v in body.items() if k not in ("body_v", "source_answer_meta")}
    assert dossiers.shown(old)["summary_state"] == "legacy_unknown"
    lying = dict(body, summary=None)  # the state says shown, the body has no summary
    assert dossiers.shown(lying)["summary_state"] == "legacy_unknown"
    removed = build_body(with_meta("removed"), ["c1", "c2", "c3", "c4"])
    assert dossiers.shown(dict(removed, summary="A summary that is there"))["summary_state"] == "legacy_unknown"


def test_a_legacy_source_builds_and_exports_as_before_with_the_neutral_sentence_p14():
    record = with_meta("removed")
    record.pop("answer_meta")
    body = build_body(record, ["c1", "c2", "c3", "c4"])
    assert body["source_answer_meta"] == {"check": "legacy_unknown"}
    frozen = dossiers.freeze(body, version=2, created_at="2026-10-07T11:00:00+02:00", ticks={})
    page = dossiers.render_html(frozen)
    assert SENTENCES["neutral"] in page and frozen["source_answer_meta"] == {"check": "legacy_unknown"}


def test_a_frozen_dossier_says_why_its_summary_is_not_there_p14():
    body = build_body(with_meta("removed"), ["c1", "c2", "c3", "c4"])
    frozen = dossiers.freeze(body, version=2, created_at="2026-10-07T11:00:00+02:00", ticks={})
    assert SENTENCES["removed"] in dossiers.render_html(frozen)
    partial = dossiers.freeze(build_body(with_meta("shown"), ["c1"]), version=2, created_at="2026-10-07T11:00:00+02:00",
                              ticks={})
    assert "not every finding is kept" in dossiers.render_html(partial)


def test_dossier_routes_keep_the_state_through_create_and_edit_p14(agent):
    record = with_meta("removed")
    hold(record)
    made = agent.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()
    assert made["body_v"] == 2 and made["source_answer_meta"]["check"] == "verified" and made["summary_state"] == "removed"
    edited = agent.client.put(f"/api/dossiers/{made['dossier_id']}", json={"title": "new", "from_version": 1}).json()
    assert edited["source_answer_meta"]["check"] == "verified" and edited["version"] == 2


# privacy.project_record is a boundary too: it never hands a raw stored answer_meta to a reader (C1 condition 1).
STORED_KEYS = ("digest", "bound", "ask_id", "check_run_id")


@pytest.mark.parametrize("hidden", [False, True])
def test_project_record_never_passes_a_raw_stored_state_through_p06(hidden):
    record = with_meta("shown")
    assert "digest" in record["answer_meta"]
    store = RouteStore(hide={"c_hid"} if hidden else set())
    shown = privacy.project_record(record, store, privacy.read_hidden(store))
    assert shown["answer_meta"] == wire_of(record) and shown["answer_meta"]["check"] == "verified"
    for key in STORED_KEYS:
        assert key not in shown["answer_meta"]
    assert ("privacy" in shown) is hidden


def test_project_record_does_not_believe_a_stored_value_that_is_shaped_like_the_wire_p06():
    record = with_meta("shown")
    record["answer_meta"] = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
                             "summary": {"state": "removed", "removals": [("first_check", "K6")], "rewrite": "not_attempted"}}
    shown = privacy.project_record(record, RouteStore(hide=set()), privacy.read_hidden(RouteStore(hide=set())))
    assert shown["answer_meta"]["check"] != "verified" or answer_state.check_wire(shown) is None


def test_project_record_leaves_a_record_with_no_state_key_alone_p11():
    record = ask_record()
    store = RouteStore(hide=set())
    assert "answer_meta" not in privacy.project_record(record, store, privacy.read_hidden(store))


# The release smoke on a real Ask: the real producer, the real execute and f42-api's own GET, no double (finding 1).
def release_smoke():
    """The smoke that gates the release. core.api.smoke once it holds the typed-state check; until then the copy of
    wave8/release (2f35df9) kept beside the tests, byte for byte."""
    import importlib.util
    from pathlib import Path

    from core.api import smoke
    if hasattr(smoke, "_state_problem"):
        return smoke
    spec = importlib.util.spec_from_file_location("release_smoke", Path(__file__).with_name("release_smoke.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_release_smoke_passes_a_real_ask_read_through_f42_api_live_and_stored_e2e(api):
    smoke = release_smoke()
    r = api.client.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                        "market": "ZA", "wait": True})
    assert r.status_code == 200
    ask_id = r.json()["ask_id"]
    live = api.client.get(f"/api/ask/{ask_id}", headers=GOOD).json()
    assert live["answer_meta"]["check"] == "verified"
    assert smoke.check_ask_record(live)[0] is True, smoke.check_ask_record(live)
    row = json.loads(agent_app.SINK[-1]["record"])
    agent_app.ASKS.clear()
    api.store.records[ask_id] = row  # the runs row, as the store would hand it back
    stored = api.client.get(f"/api/ask/{ask_id}", headers=GOOD).json()
    assert stored["answer_meta"] == live["answer_meta"]
    ok, evidence = smoke.check_ask_record(stored)
    assert ok is True, evidence
    assert "summary shown" in evidence


def test_the_release_smoke_passes_a_removed_summary_the_producer_explains_e2e(api, monkeypatch):
    from core.agent import checks, plain
    smoke = release_smoke()
    gap = plain.gap(checks._code_gap("Short answer removed", "K6", "the short answer text"))

    def producer(request, emit, should_stop):  # the pipeline's result for a first-check removal
        record = ask_record()
        answer = record["answer"]
        answer.update(status="partial", short_answer="", gaps=[gap])
        run = {"run_id": "r_" + request["ask_id"][2:], "posts": 3}
        meta = answer_state.build(ask_id=request["ask_id"], check_run_id=run["run_id"], execution_state="completed",
                                  stop_reason=None, summary_state="removed", removals=[("first_check", "K6")],
                                  rewrite="not_attempted", answer=answer)
        return {"answer": answer, "run": run, "answer_meta": meta}

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: producer)
    r = api.client.post("/api/ask", headers=GOOD, json={"question": "What is amapiano doing?", "market": "ZA",
                                                        "wait": True})
    got = api.client.get(f"/api/ask/{r.json()['ask_id']}", headers=GOOD).json()
    assert got["answer"]["short_answer"] == "" and got["answer_meta"]["summary"]["state"] == "removed"
    ok, evidence = smoke.check_ask_record(got)
    assert ok is True, evidence


def test_the_release_smoke_refuses_the_same_ask_when_no_state_reaches_f42_api_e2e(api, monkeypatch):
    smoke = release_smoke()
    real = agent_app.fixture_agent
    monkeypatch.setattr(agent_app, "resolve_run_ask",
                        lambda: lambda req, emit, stop: {k: v for k, v in real(req, emit, stop).items() if k != "answer_meta"})
    r = api.client.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                        "market": "ZA", "wait": True})
    got = api.client.get(f"/api/ask/{r.json()['ask_id']}", headers=GOOD).json()
    assert got["answer_meta"] == {"check": "legacy_unknown"}
    ok, why = smoke.check_ask_record(got)
    assert ok is False and "no verified answer state" in why


# A scheduled ask keeps the state through the pinned wrapper, and the ids agree (P-18).
def test_a_scheduled_ask_keeps_its_state_and_the_schedule_run_id_p18(agent, monkeypatch):
    from core.api import scheduled
    rid = "sched-2026-10-07-s_a"

    def producer(request, emit, should_stop):
        record, kw = make("shown")
        run = {"run_id": request["run_id"], "posts": 3}
        return {"answer": record["answer"], "run": run,
                "answer_meta": answer_state.build(ask_id=request["ask_id"], check_run_id=request["run_id"],
                                                  answer=record["answer"], **kw)}

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: producer)
    held = scheduled.live_ask({"schedule_id": "s_a", "question": "q", "market": "ZA", "tier": "T0"}, rid)
    assert held["status"] == "complete" and held["run"]["run_id"] == rid
    assert held["answer_meta"]["check_run_id"] == rid and answer_state.verify_stored(held) is None
    assert answer_state.meta_view(held)["check"] == "verified"
    assert json.loads(agent_app.SINK[-1]["record"])["answer_meta"]["check_run_id"] == rid


# P-19: F42_FIXTURE_STATE makes the fixture agent return a fixture of C1 v2 section 7. The expected states are typed
# here from the catalogue, never read back from the module that builds them.
CATALOGUE = {  # id: (execution, stop_reason, summary state, removals, rewrite, record status, answer status)
    "F01": ("completed", None, "shown", [], "not_attempted", "complete", "complete"),
    "F02": ("completed", None, "shown_rewritten", [("support_check", "claim_cut")], "kept", "complete", "partial"),
    "F03": ("completed", None, "shown_rewritten", [("support_check", "claim_narrowed")], "kept", "complete", "complete"),
    "F04": ("completed", None, "fixed_text", [], "not_attempted", "complete", "insufficient_evidence"),
    "F06": ("stopped_on_budget", "budget_full", "fixed_text", [], "not_attempted", "complete", "insufficient_evidence"),
    "F07": ("stopped_on_budget", "model_call_unverified", "fixed_text", [], "not_attempted", "complete",
            "insufficient_evidence"),
    "F08": ("refused_budget_spent", None, "fixed_text", [], "not_attempted", "complete", "insufficient_evidence"),
    "F10": ("completed", None, "removed", [("first_check", "K6")], "not_attempted", "complete", "partial"),
    "F11": ("completed", None, "removed", [("first_check", "K2")], "not_attempted", "complete", "partial"),
    "F12a": ("completed", None, "removed", [("support_check", "claim_cut")], "empty", "complete", "partial"),
    "F12b": ("completed", None, "removed", [("support_check", "claim_cut")], "call_failed", "complete", "partial"),
    "F12c": ("completed", None, "removed", [("support_check", "claim_cut")], "no_budget", "complete", "partial"),
    "F13": ("completed", None, "removed", [("support_check", "claim_cut"), ("support_check", "claim_narrowed")],
            "repeated_removed_claim", "complete", "partial"),
    "F14": ("completed", None, "removed", [("support_check", "claim_cut"), ("recheck", "K6")], "removed_after_check",
            "complete", "partial"),
    "F15a": ("completed", None, "removed", [("support_check", "claim_cut"), ("field_check", "K9")],
             "removed_after_check", "complete", "partial"),
    "F15b": ("completed", None, "removed", [("field_check", "K6")], "not_attempted", "complete", "partial"),
    "F16": ("completed", None, "removed", [("field_check", "field_unchecked")], "not_attempted", "complete", "partial"),
    "F17": ("completed", None, "removed", [("support_check", "claim_cut"), ("critic", "claim_cut")],
            "removed_after_check", "complete", "partial"),
    "F18": ("completed", None, "blank_unexplained", [], "not_attempted", "complete", "complete"),
}


def ask_with_fixture(api, monkeypatch, fixture_id):
    monkeypatch.setenv("F42_FIXTURE_STATE", fixture_id)
    r = api.client.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                        "market": "ZA", "wait": True})
    return r.json()


@pytest.mark.parametrize("fixture_id", sorted(CATALOGUE))
def test_the_fixture_agent_returns_the_state_the_catalogue_names_p19(api, monkeypatch, fixture_id):
    from core.agent.answer import validate_answer
    execution, stop_reason, summary, removals, rewrite, status, answer_status = CATALOGUE[fixture_id]
    got = ask_with_fixture(api, monkeypatch, fixture_id)
    meta = got["answer_meta"]
    assert meta["check"] == "verified" and answer_state.check_wire(got) is None
    assert (meta["execution"]["state"], meta["execution"]["stop_reason"]) == (execution, stop_reason)
    assert meta["summary"] == {"state": summary, "removals": [{"stage": s, "cause": c} for s, c in removals],
                               "rewrite": rewrite}
    assert got["status"] == status and got["answer"]["status"] == answer_status
    assert validate_answer(got["answer"]) == []
    stored = json.loads(agent_app.SINK[-1]["record"])
    assert answer_state.verify_stored(stored) is None and stored["answer_meta"]["check_run_id"] == got["run"]["run_id"]


@pytest.mark.parametrize("fixture_id", sorted(CATALOGUE))
def test_the_release_smoke_reads_each_fixture_as_the_catalogue_says_p19(api, monkeypatch, fixture_id):
    smoke = release_smoke()
    got = ask_with_fixture(api, monkeypatch, fixture_id)
    ok, why = smoke.check_ask_record(api.client.get(f"/api/ask/{got['ask_id']}", headers=GOOD).json())
    assert ok is (fixture_id != "F18"), why  # a blank summary nobody explained is the one the smoke refuses


@pytest.mark.parametrize("fixture_id", ["F19", "F20"])
def test_the_legacy_fixtures_carry_no_state_and_read_as_legacy_p19(api, monkeypatch, fixture_id):
    smoke = release_smoke()
    got = ask_with_fixture(api, monkeypatch, fixture_id)
    assert got["answer_meta"] == {"check": "legacy_unknown"} and got["answer"]["short_answer"] == ""
    assert "answer_meta" not in json.loads(agent_app.SINK[-1]["record"])
    ok, why = smoke.check_ask_record(got)
    assert ok is False and "no verified answer state" in why


def test_f09_fails_the_run_and_stores_the_failed_state_and_an_unknown_id_fails_it_too_p19(api, monkeypatch):
    got = ask_with_fixture(api, monkeypatch, "F09")
    assert got["status"] == "failed" and got["answer_meta"]["execution"]["state"] == "failed"
    for fixture_id in ("F05", "F99"):
        got = ask_with_fixture(api, monkeypatch, fixture_id)
        assert got["status"] == "failed", fixture_id


def test_without_the_variable_the_fixture_agent_is_what_it_was_p19(api, monkeypatch):
    monkeypatch.delenv("F42_FIXTURE_STATE", raising=False)
    got = api.client.post("/api/ask", headers=GOOD, json={"question": "What is behind #fixture in South Africa this week?",
                                                          "market": "ZA", "wait": True}).json()
    assert got["answer_meta"]["summary"]["state"] == "shown" and got["answer"]["status"] == "complete"


# C1 3 item 8: the metadata code runs in its own try block, so a fault in it can fail neither a paid answer nor the
# record of a failed run.
def test_a_fault_in_keeping_the_state_never_fails_the_answer_or_the_failed_record_p02b(agent, monkeypatch, caplog):
    record, kw = make("shown")

    def boom(*_args, **_kwargs):
        raise RuntimeError("keep broke")

    monkeypatch.setattr(agent_app, "keep_state", boom)
    with caplog.at_level(logging.ERROR, logger="f42-agent"):
        ask = run_execute({"answer": record["answer"], "run": record["run"], "answer_meta": meta_for(record, kw)})
    held = ask.snapshot()
    assert held["status"] == "complete" and held["answer"] == record["answer"] and "answer_meta" not in held
    assert ask.finished.is_set() and agent_app.SINK and "answer_meta" in caplog.text
    failed = run_execute(raises=RuntimeError("the agent broke")).snapshot()
    assert failed["status"] == "failed" and failed["error"]["error"] == "internal"


def test_a_run_that_returns_no_state_logs_no_error_p02(agent, caplog):
    record, _ = make("shown")
    with caplog.at_level(logging.DEBUG, logger="f42-agent"):
        run_execute({"answer": record["answer"], "run": record["run"]})
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


# An a80 agent knows nothing of the state: its records read as legacy on every live read (the live half of P-11).
def test_an_older_agents_live_read_wait_post_and_investigation_read_as_legacy_p11(api, monkeypatch):
    from core.api.tests.test_privacy_ask_routes import older_agent
    record = ask_record()
    record["run"] = {"run_id": "r_x"}
    inv = {"investigation_id": "i_0123456789ab", "status": "complete", "question": "q", "plan": {}, "record": record}
    payloads = {f"/api/ask/{ASK}": (200, record), "/api/ask": (200, record),
                "/api/investigations/i_0123456789ab": (200, inv)}
    monkeypatch.setattr(core.api, "agent_app", types.SimpleNamespace(app=older_agent(payloads)), raising=False)
    live = api.client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    posted = api.client.post("/api/ask", headers=GOOD, json={"question": "q", "wait": True}).json()
    investigated = api.client.get("/api/investigations/i_0123456789ab", headers=GOOD).json()["record"]
    for got in (live, posted, investigated):
        assert got["answer_meta"] == {"check": "legacy_unknown"}


# The hop from f42-agent: only a well formed wire value that fits its record is passed on (C1 5.2).
def test_the_agent_hop_keeps_a_well_formed_value_and_refuses_the_rest():
    from core.api import summary_state
    record = with_meta("shown")

    def hop(meta):
        return summary_state.with_wire({**record, "answer_meta": meta}, from_agent=True)["answer_meta"]

    assert hop({"check": "legacy_unknown"}) == {"check": "legacy_unknown"}
    assert hop({"check": "legacy_unknown", "extra": 1}) == {"check": "unverified", "problem": "shape"}
    assert hop({"check": "unverified", "problem": "digest"}) == {"check": "unverified", "problem": "digest"}
    for bad in ({"check": "unverified"}, {"check": "unverified", "problem": 7},
                {"check": "unverified", "problem": "digest", "extra": 1}):
        assert hop(bad) == {"check": "unverified", "problem": "shape"}, bad
    assert hop(wire_of(record)) == wire_of(record)


def test_a_fault_in_the_state_code_reads_as_unverified_with_a_listed_problem_code(monkeypatch):
    from core.api import summary_state
    listed = {"while_running", "shape", "forbidden_key", "version", "enum", "digest", "ids", "bound", "pairing",
              "reasons"}  # the codes of C1 5.1
    assert summary_state.UNAVAILABLE == {"check": "unverified", "problem": "shape"}
    assert summary_state.UNAVAILABLE["problem"] in listed
    record = with_meta("shown")

    def boom(_record):
        raise RuntimeError("broke")

    with monkeypatch.context() as patch:
        patch.setattr(answer_state, "meta_view", boom)
        assert summary_state.wire(record) == summary_state.UNAVAILABLE
        assert summary_state.with_wire(record)["answer_meta"] == summary_state.UNAVAILABLE
    monkeypatch.setattr(answer_state, "check_wire", boom)
    sent = {**record, "answer_meta": wire_of(record)}
    assert summary_state.with_wire(sent, from_agent=True)["answer_meta"] == summary_state.UNAVAILABLE
    assert summary_state.with_wire("not a record") == "not a record"
    assert summary_state.with_wire({"x": 1}) == {"x": 1}


# The sentence a reader gets for each removal, written here from C1 2.6 (never read back from the module).
CUT = "The one-line summary was removed after a claim it may have rested on did not pass its checks."
PLACE = ("The one-line summary was removed because it named a place no cited post is located in, or relied on a post "
         "outside the question's window or market.")
TERM = "The one-line summary was removed because it used a term or source the trust rules do not allow."
REMOVAL_SENTENCES = {
    ("first_check", "K2"): "The one-line summary was removed because it used a figure that no checked finding holds.",
    ("first_check", "K3"): PLACE,
    ("first_check", "K6"): TERM,
    ("recheck", "K6"): TERM,
    ("first_check", "K8"): "The one-line summary was removed because it quoted words that are not in the posts it cites.",
    ("recheck", "K9"): ("The one-line summary was removed because it made a forecast, and forecasts stay held until "
                        "they beat a simple no-change forecast."),
    ("support_check", "claim_cut"): CUT,
    ("critic", "claim_cut"): CUT,
    ("support_check", "claim_narrowed"): ("The one-line summary was removed after a claim it may have rested on was "
                                          "narrowed."),
    ("field_check", "K6"): ("The one-line summary was removed because the text check found it describes people in a "
                            "way the trust rules do not allow."),
    ("field_check", "K3"): PLACE,
    ("field_check", "field_unchecked"): ("The one-line summary was too long to check with the posts it rests on, so it "
                                         "was left out."),
    ("first_check", "unattributed"): "The one-line summary was removed by the checks.",
}
AFTER_CHECK = " A rewritten summary did not pass the checks either."


def removed_record(removals, rewrite="not_attempted"):
    record, kw = make("removed")
    record["answer_meta"] = meta_for(record, kw, removals=removals, rewrite=rewrite)
    return record


@pytest.mark.parametrize("removal", sorted(REMOVAL_SENTENCES))
def test_each_removal_is_explained_in_its_own_words_in_the_export_and_the_dossier_p13(removal):
    from core.api import summary_state
    record = removed_record([removal])
    assert summary_state.summary_sentence(wire_of(record)) == REMOVAL_SENTENCES[removal]
    assert REMOVAL_SENTENCES[removal] in html.unescape(short_answer_section(export.render_answer_html(record)))
    body = build_body(record, ["c1", "c2", "c3", "c4"])
    frozen = dossiers.freeze(body, version=2, created_at="2026-10-07T11:00:00+02:00", ticks={})
    assert REMOVAL_SENTENCES[removal] in html.unescape(dossiers.render_html(frozen))


def test_a_rewrite_that_failed_its_checks_adds_its_sentence_and_only_then_p13():
    from core.api import summary_state
    for removals in ([("recheck", "K6")], [("support_check", "claim_cut"), ("field_check", "K9")],
                     [("support_check", "claim_cut"), ("critic", "claim_cut")]):
        record = removed_record(removals, "removed_after_check")
        first = REMOVAL_SENTENCES[removals[0]]
        assert summary_state.summary_sentence(wire_of(record)) == first + AFTER_CHECK
        assert first + AFTER_CHECK in html.unescape(short_answer_section(export.render_answer_html(record)))
    plain = removed_record([("support_check", "claim_cut")], "empty")
    assert AFTER_CHECK not in summary_state.summary_sentence(wire_of(plain))


# summary_state and source_answer_meta are structure: no handle can rewrite an enum word in them.
def test_a_dossier_view_keeps_its_summary_state_words_under_a_skin_mask_p12():
    from core.api import skins
    assert {"summary_state", "source_answer_meta", "answer_meta"} <= skins.STRUCTURAL
    body = build_body(with_meta("removed"), ["c1", "c2", "c3", "c4"])
    view = dossiers.shown(body)
    assert view["summary_state"] == "removed" and view["source_answer_meta"]["summary"]["state"] == "removed"
    view["evidence"].append({"id": "tiktok_1", "platform": "tiktok", "handle": "removed", "url": "u",
                             "posted_at": "2026-10-05T09:00:00+02:00", "market": "ZA", "text": "x"})
    masked = skins.mask_people(view, [], [])
    assert masked["summary_state"] == "removed" and masked["source_answer_meta"] == view["source_answer_meta"]
