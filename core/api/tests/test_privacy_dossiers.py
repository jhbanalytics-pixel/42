"""Current suppression on dossiers and the acting paths that copy an answer (C5 v2 rows A22 to A31, A40, A41).

A dossier body is stored verbatim; every read projects it with the list read now, and every path that builds a new
copy refuses what the list no longer lets it keep."""
import copy
import json
import types

import pytest
from fastapi.testclient import TestClient

from core.api import agent_app, digest, dossiers, privacy
from core.api.tests.test_privacy_ask_routes import ASK, RouteStore, hold
from core.api.tests.test_privacy_projection import (LEAK, P_HID1, P_VIS1, ask_all_hidden, ask_record, body_of, ev,
                                                    leaks)

P_VIS2 = "obs1_" + "2" * 32
K_NAMING = {"id": "k1", "text": "Posts by @hid_handle grew", "label": "observed", "kind": "observation",
            "evidence_ids": [P_VIS1]}
K_PLAIN = {"id": "k2", "text": "Amapiano grew again", "label": "observed", "kind": "observation",
           "evidence_ids": [P_VIS2]}


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.delenv("F42_DATA", raising=False)
    store = RouteStore(hide=set())
    store.posts["" + P_VIS2] = ("c_vis", "2026-10-05")
    monkeypatch.setattr("core.api.store.get_store", lambda: store)
    for held in (agent_app.SINK, agent_app.ASKS, agent_app.DOSSIER_VERSIONS, agent_app.DOSSIER_REVIEWS):
        held.clear()
    yield types.SimpleNamespace(store=store, client=TestClient(agent_app.app))
    for held in (agent_app.SINK, agent_app.ASKS, agent_app.DOSSIER_VERSIONS, agent_app.DOSSIER_REVIEWS):
        held.clear()


def source(extra_claims=(), extra_evidence=()):
    record = ask_record()
    record["answer"]["evidence"] += list(extra_evidence)
    record["answer"]["claims"] += list(extra_claims)
    return record


def make_dossier(world, keep=None, record=None):
    hold(record or source())
    body = {"from": {"ask_id": ASK}}
    if keep is not None:
        body["keep"] = keep
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}, **({"keep": keep} if keep else {})})
    return r


def versions(dossier_id):
    return [json.loads(v["body"]) if isinstance(v["body"], str) else v["body"]
            for v in agent_app.DOSSIER_VERSIONS if v["dossier_id"] == dossier_id]


def draft_before_the_hide(world, keep=("c1", "c2", "c3")):
    """A draft made while nobody was hidden; the hide comes after (case T1)."""
    hold(source())
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert r.status_code == 201
    did = r.json()["dossier_id"]
    r = world.client.put(f"/api/dossiers/{did}", json={"keep": list(keep), "from_version": 1})
    assert r.status_code == 200
    return did


# Create (A22).
def test_a_dossier_made_after_the_hide_keeps_only_what_the_projection_leaves_a22(world):
    world.store.hide = {"c_hid"}
    hold(source())
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert r.status_code == 201
    stored = versions(r.json()["dossier_id"])[0]
    assert [c["claim_id"] for c in stored["claims"] if c["kept"]] == ["c1", "c4"]
    assert P_HID1 not in json.dumps([c for c in stored["claims"] if c["kept"]])
    assert [e["id"] for e in stored["evidence"]] == [P_VIS1, "obs1_" + "c" * 32]


def test_the_default_for_a_new_dossier_leaves_out_what_the_list_no_longer_lets_it_keep_a22(world):
    world.store.hide = {"c_hid"}
    hold(source())
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    kept = [c["claim_id"] for c in versions(r.json()["dossier_id"])[0]["claims"] if c["kept"]]
    assert kept == ["c1", "c4"] and not {"c2", "c3"} & set(kept)


def test_a_dossier_cannot_be_made_while_the_list_is_unreadable_a22(world):
    world.store.fail = "list"
    hold(source())
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"
    assert agent_app.DOSSIER_VERSIONS == []


# Edit (A23).
def test_an_edit_cannot_keep_a_claim_that_now_rests_on_a_hidden_post_and_earlier_versions_stay_a23(world):
    did = draft_before_the_hide(world)
    before = copy.deepcopy(agent_app.DOSSIER_VERSIONS)
    world.store.hide = {"c_hid"}
    refused = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1", "c3"], "from_version": 2})
    assert refused.status_code == 409 and "c3" in refused.json()["message"]
    assert agent_app.DOSSIER_VERSIONS == before
    ok = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "from_version": 2})
    assert ok.status_code == 200 and ok.json()["version"] == 3
    assert agent_app.DOSSIER_VERSIONS[:2] == before[:2]


def test_an_edit_with_no_keep_is_refused_while_a_kept_claim_can_no_longer_be_kept_a23(world):
    """The edit would carry the earlier choice forward; it must not quietly drop a claim the reader chose. It names
    the claims and says how to go on, and appends nothing."""
    did = draft_before_the_hide(world)
    world.store.hide = {"c_hid"}
    before = copy.deepcopy(agent_app.DOSSIER_VERSIONS)
    r = world.client.put(f"/api/dossiers/{did}", json={"title": "A new title", "from_version": 2})
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    assert "c2" in r.json()["message"] and "c3" in r.json()["message"] and "c1" not in r.json()["message"]
    assert "keep" in r.json()["message"] and "42 no longer shows" in r.json()["message"]
    assert agent_app.DOSSIER_VERSIONS == before
    ok = world.client.put(f"/api/dossiers/{did}", json={"title": "A new title", "keep": ["c1"], "from_version": 2})
    assert ok.status_code == 200
    assert [c["claim_id"] for c in versions(did)[-1]["claims"] if c["kept"]] == ["c1"]


def test_an_edit_with_no_keep_goes_through_when_every_kept_claim_can_still_be_kept_a23(world):
    did = draft_before_the_hide(world)
    r = world.client.put(f"/api/dossiers/{did}", json={"title": "A new title", "from_version": 2})
    assert r.status_code == 200
    assert [c["claim_id"] for c in versions(did)[-1]["claims"] if c["kept"]] == ["c1", "c2", "c3"]


# Freeze (A24).
def test_a_draft_made_before_the_hide_cannot_freeze_until_the_suppression_is_lifted_a24(world):
    did = draft_before_the_hide(world, keep=("c1", "c2"))
    for cid in ("c2",):
        assert world.client.post(f"/api/dossiers/{did}/ticks", json={"claim_id": cid, "ticked": True}).status_code == 201
    world.store.hide = {"c_hid"}
    r = world.client.post(f"/api/dossiers/{did}/freeze", json={})
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    assert [c["claim_id"] for c in r.json()["claims"]] == ["c2"] and "42 no longer shows" in r.json()["claims"][0]["reason"]
    world.store.hide = set()
    assert world.client.post(f"/api/dossiers/{did}/freeze", json={}).status_code == 201


def test_freeze_with_an_unreadable_list_is_refused_and_appends_nothing_a24(world):
    did = draft_before_the_hide(world, keep=("c1",))
    before = len(agent_app.DOSSIER_VERSIONS)
    world.store.fail = "list"
    r = world.client.post(f"/api/dossiers/{did}/freeze", json={})
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"
    assert len(agent_app.DOSSIER_VERSIONS) == before


# Reads (A25, A26, A27, A28, A34, A36, A37).
def frozen_dossier(world):
    did = draft_before_the_hide(world, keep=("c1", "c2"))
    world.client.post(f"/api/dossiers/{did}/ticks", json={"claim_id": "c2", "ticked": True, "note": "checked @hid_handle"})
    assert world.client.post(f"/api/dossiers/{did}/freeze", json={}).status_code == 201
    return did


def test_the_current_version_shows_withheld_slots_and_keeps_the_stored_body_a25_a34(world):
    did = frozen_dossier(world)
    stored = copy.deepcopy(agent_app.DOSSIER_VERSIONS)
    world.store.hide = {"c_hid"}
    r = world.client.get(f"/api/dossiers/{did}")
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    by = {c["claim_id"]: c for c in body["claims"]}
    assert by["c2"]["text"] == privacy.CLAIM_WITHHELD and by["c1"]["text"] == "Amapiano posts rose"
    assert leaks(body, [P_HID1]) == [] and body["privacy"]["state"] == "applied"
    assert body["content_hash"] == agent_app.DOSSIER_VERSIONS[-1]["content_hash"]
    assert agent_app.DOSSIER_VERSIONS == stored


def test_every_version_is_projected_on_its_own_a26(world):
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    for n in (1, 2, 3):
        body = world.client.get(f"/api/dossiers/{did}/versions/{n}").json()
        assert leaks(body, [P_HID1]) == [], n
    frozen = world.client.get(f"/api/dossiers/{did}/versions/3").json()
    assert frozen["state"] == "frozen" and frozen["frozen_from"] == 2


def test_the_list_masks_titles_and_questions_and_keeps_counts_a27(world):
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    rows = world.client.get("/api/dossiers").json()["dossiers"]
    assert leaks(rows) == [] and rows[0]["dossier_id"] == did and rows[0]["frozen_version"] == 3 and rows[0]["kept"] == 2


def test_a_lifted_suppression_shows_the_dossier_as_stored_a37(world):
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    assert world.client.get(f"/api/dossiers/{did}").json()["claims"][1]["withheld"] is True
    world.store.hide = set()
    again = world.client.get(f"/api/dossiers/{did}").json()
    assert "privacy" not in again and again["claims"][1]["text"] == "The hidden creator led"


def test_an_unreadable_list_withholds_every_claim_of_a_dossier_read_u(world):
    did = frozen_dossier(world)
    world.store.fail = "list"
    body = world.client.get(f"/api/dossiers/{did}").json()
    assert all(c["withheld"] for c in body["claims"]) and body["privacy"]["state"] == "unavailable"


def test_a_note_naming_a_hidden_creator_is_masked_in_the_view_and_stored_verbatim_a28(world):
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    body = world.client.get(f"/api/dossiers/{did}/versions/3").json()
    assert "hid_handle" not in body_of(body["reviews"]) and "hid_handle" not in body_of(body["ticks"])
    assert any(row["note"] == "checked @hid_handle" for row in agent_app.DOSSIER_REVIEWS)


# Exports (A29, A30).
def test_a_frozen_export_is_clean_carries_the_withheld_words_and_a_notice_a29(world):
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    r = world.client.get(f"/api/dossiers/{did}/versions/3/export")
    assert r.status_code == 200 and leaks(r.text, [P_HID1]) == []
    assert privacy.CLAIM_WITHHELD in r.text and privacy.EXPORT_NOTICE in r.text


def test_a_draft_export_is_still_refused_a29(world):
    did = draft_before_the_hide(world)
    world.store.hide = {"c_hid"}
    assert world.client.get(f"/api/dossiers/{did}/versions/2/export").status_code == 409


def test_an_export_with_an_unreadable_list_is_refused_a29(world):
    did = frozen_dossier(world)
    world.store.fail = "list"
    r = world.client.get(f"/api/dossiers/{did}/versions/3/export")
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"


def test_the_page_handed_to_the_pdf_renderer_is_the_projected_page_a30(world, monkeypatch):
    from core.api import pdf
    did = frozen_dossier(world)
    world.store.hide = {"c_hid"}
    seen = []
    monkeypatch.setattr(pdf, "render_pdf", lambda page: seen.append(page) or b"%PDF-1.4 stub")
    r = world.client.get(f"/api/dossiers/{did}/versions/3/export?format=pdf")
    assert r.status_code == 200 and len(seen) == 1
    assert leaks(seen[0], [P_HID1]) == [] and privacy.EXPORT_NOTICE in seen[0]


# What a dossier may keep, and a lift (A41).
def test_a_claim_whose_words_name_a_hidden_creator_is_not_selectable_until_the_lift_a41(world):
    record = source([K_NAMING, K_PLAIN], [ev(P_VIS2, "vis_handle", "more visible words")])
    hold(record)
    world.store.hide = {"c_hid"}
    made = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert made.status_code == 201
    did = made.json()["dossier_id"]
    stored = versions(did)[0]
    assert [c["claim_id"] for c in stored["claims"] if c["kept"]] == ["c1", "c4", "k2"]
    assert next(c for c in stored["claims"] if c["claim_id"] == "k2")["text"] == "Amapiano grew again"  # verbatim
    refused = world.client.put(f"/api/dossiers/{did}", json={"keep": ["k1", "k2"], "from_version": 1})
    assert refused.status_code == 409 and "k1" in refused.json()["message"]
    world.store.hide = set()
    ok = world.client.put(f"/api/dossiers/{did}", json={"keep": ["k1", "k2"], "from_version": 1})
    assert ok.status_code == 200
    assert "differs from the source" not in world.client.post(f"/api/dossiers/{did}/freeze", json={}).text


# A finding saved from an answer (A31).
def test_a_finding_cannot_be_saved_from_an_answer_citing_a_hidden_post_a31(world):
    world.store.hide = {"c_hid"}
    world.store.records[ASK] = ask_record()
    r = world.client.post("/api/findings", json={"from": {"ask_id": ASK}})
    assert r.status_code == 409 and "42 no longer shows" in r.json()["message"]


def test_a_finding_cannot_be_saved_while_the_list_is_unreadable_a31(world):
    world.store.fail = "list"
    world.store.records[ASK] = ask_record()
    r = world.client.post("/api/findings", json={"from": {"ask_id": ASK}})
    assert r.status_code == 503 and r.json()["error"] == "people_unavailable"


# The digest (A40).
def alert_card():
    return {"item_id": "i1", "market": "ZA", "title": "A topic", "count_line": "3 creators", "state_word": "Rising",
            "evidence": [ev("tt_1", "hid_handle", "fixture hidden words one"),
                         ev("tt_3", "vis_handle", "visible fixture words"), ev("tt_2", "hid_handle", "two")]}


def digest_resp():
    return {"date": "2026-10-07", "alerts": [{"watch_id": "w1", "label": "Watch", "market": "ZA", "item_id": "i1",
                                              "fired_because": "It rose", "card": alert_card()}], "waiting": []}


def test_a_digest_names_no_hidden_creator_and_keeps_the_link_of_a_visible_one_a40():
    store = RouteStore(hide={"c_hid"})
    resp = privacy.withhold_digest(digest_resp(), store)
    html, text = digest.render_html(resp, "https://app.example"), digest.render_text(resp, "https://app.example")
    for body in (html, text):
        assert "hid_handle" not in body and "fixture hidden words" not in body
        assert "A post on TikTok" in body
        assert "vis_handle" in body and "/@vis_handle/video/1" in body


def test_a_digest_with_an_unreadable_list_carries_no_post_handle_or_link_at_all_a40():
    resp = privacy.withhold_digest(digest_resp(), RouteStore(fail="list"))
    for body in (digest.render_html(resp, "https://app.example"), digest.render_text(resp, "https://app.example")):
        assert "vis_handle" not in body and "hid_handle" not in body and "/video/" not in body
        assert "A post on TikTok" in body


def test_a_digest_with_nobody_hidden_is_the_digest_it_was_a37():
    store = RouteStore(hide=set())
    resp = privacy.withhold_digest(digest_resp(), store)
    assert resp == digest_resp()


def test_the_built_digest_is_withheld_not_only_its_renderers_a40(monkeypatch):
    monkeypatch.setattr(digest.discover, "build_alerts", lambda store, watches, date=None: digest_resp())
    out = digest.build(RouteStore(hide={"c_hid"}), [], "2026-10-07", "https://app.example")
    assert "hid_handle" not in out["html"] and "hid_handle" not in out["text"]
    assert "A post on TikTok" in out["text"]


# The app's own edit sends the title and notes it was shown, which are the masked view (dossiers42.jsx).
def named_draft(world):
    hold(source())
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    r = world.client.put(f"/api/dossiers/{did}", json={
        "keep": ["c1"], "title": "Amapiano and @hid_handle", "notes": {"c1": "ask @hid_handle about this"},
        "from_version": 1})
    assert r.status_code == 200
    return did


def test_an_edit_that_sends_back_the_masked_title_and_notes_keeps_the_stored_words_a37(world):
    did = named_draft(world)
    world.store.hide = {"c_hid"}
    view = world.client.get(f"/api/dossiers/{did}").json()
    assert "hid_handle" not in view["title"] and "hid_handle" not in body_of(view["claims"][0]["note"])
    notes = {c["claim_id"]: c["note"] for c in view["claims"] if c.get("note")}
    r = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "title": view["title"], "notes": notes,
                                                       "from_version": view["version"]})
    assert r.status_code == 200
    stored = versions(did)[-1]
    assert stored["title"] == "Amapiano and @hid_handle"
    assert next(c for c in stored["claims"] if c["claim_id"] == "c1")["note"] == "ask @hid_handle about this"
    world.store.hide = set()  # the lift restores what the reader wrote
    again = world.client.get(f"/api/dossiers/{did}").json()
    assert again["title"] == "Amapiano and @hid_handle" and "privacy" not in again


def test_words_the_reader_actually_changes_are_stored_as_typed(world):
    did = named_draft(world)
    world.store.hide = {"c_hid"}
    view = world.client.get(f"/api/dossiers/{did}").json()
    r = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "title": "A better title",
                                                       "notes": {"c1": "a new note"}, "from_version": view["version"]})
    assert r.status_code == 200
    stored = versions(did)[-1]
    assert stored["title"] == "A better title"
    assert next(c for c in stored["claims"] if c["claim_id"] == "c1")["note"] == "a new note"


# A finding is a durable shared row: it is built from what a reader may see (C5 v2 section 7, the stricter reading).
def test_a_finding_cannot_be_saved_from_a_claim_whose_words_name_a_hidden_person_a31(world):
    world.store.hide = {"c_hid"}
    record = ask_record()
    record["question"] = "What is amapiano doing in South Africa?"
    record["answer"]["claims"] = [dict(K_NAMING)]
    world.store.records[ASK] = record
    r = world.client.post("/api/findings", json={"from": {"ask_id": ASK}})
    assert r.status_code == 409 and r.json()["error"] == "not_eligible"
    assert "k1" in r.json()["message"] and "name people" in r.json()["message"]


def test_a_finding_cannot_be_saved_from_a_question_that_names_a_hidden_person_a31(world):
    world.store.hide = {"c_hid"}
    record = ask_record()
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] == "c1"]
    world.store.records[ASK] = record  # its question is "What is @hid_handle doing ..."
    r = world.client.post("/api/findings", json={"from": {"ask_id": ASK}})
    assert r.status_code == 409 and "question names a person 42 no longer shows" in r.json()["message"]


def test_a_finding_from_an_answer_that_names_nobody_hidden_goes_on_to_the_producer_checks_a31(world):
    world.store.hide = {"c_hid"}
    record = ask_record()
    record["question"] = "What is amapiano doing in South Africa?"
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] == "c1"]
    world.store.records[ASK] = record
    r = world.client.post("/api/findings", json={"from": {"ask_id": ASK}})
    assert r.json().get("error") != "not_eligible" or "42 no longer shows" not in r.json().get("message", "")
