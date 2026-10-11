"""A skin report names only the accounts it may name (contract 15.1). The comment links kept beside an answer hold ids and
nothing else; a skin run must not turn them into a way to read a creator's handle or address."""
import json

from core.api import agent_app, comment_links, skins
from core.api.tests.test_privacy_projection import PrivStore, ev

POST = "tiktok_7100"
COMMENT = "tiktok_comment_7200"
MENTIONED = "tiktok_comment_hid_handle"  # a comment id the vendor scraped, which happens to equal a handle
HANDLES = ["hid_handle", "@hid_handle", "hid_other"]


def skin_record(comment_id=COMMENT):
    post = ev(POST, "hid_handle", "words about amapiano")
    remark = {"id": comment_id, "platform": "tiktok", "handle": "hid_other", "url": "https://www.tiktok.com/@hid_handle/video/1",
              "posted_at": "2026-10-05T10:00:00+02:00", "market": "ZA", "text": "hid_handle is right", "engagement": {},
              "flags": ["market_assumed"]}
    claim = {"id": "c1", "text": "A commenter agreed.", "label": "single_source", "kind": "observation",
             "evidence_ids": [comment_id], "quotes": []}
    return {"ask_id": "a_20261009_skin0001", "question": "What about amapiano?", "market": "ZA", "status": "complete",
            "skin_id": "sk_fixture", "created_at": "2026-10-09T09:00:00+02:00", "run": {"posts": 1},
            "answer": {"status": "complete", "as_of": "2026-10-09T09:00:00+02:00", "short_answer": "Amapiano.",
                       "evidence": [post, remark], "claims": [claim], "so_what": [], "watch_next": [], "gaps": []},
            "evidence_links": {"v": 1, "parents": {comment_id: POST}}}


def named(value):
    text = json.dumps(value)
    return [h for h in HANDLES if h in text]


def test_a_skin_run_that_may_name_nobody_shows_no_handle_or_address_anywhere_in_the_record_or_its_links():
    out = skins.mask_people(skin_record(), [], [])
    assert named(out) == []
    assert out["evidence_links"] == {"v": 1, "parents": {COMMENT: POST}}


def test_the_links_are_the_same_whatever_the_skin_masks_so_the_privacy_check_can_still_use_them():
    record = skin_record()
    out = skins.mask_people(record, [], [])
    assert comment_links.read(out) == comment_links.read(record)
    assert record == skin_record()  # the stored record is not changed


def test_a_skin_run_that_may_name_the_author_keeps_the_links_and_names_only_the_author():
    out = skins.mask_people(skin_record(), ["tiktok:hid_handle"], [])
    assert out["evidence_links"] == {"v": 1, "parents": {COMMENT: POST}}
    assert "hid_other" not in json.dumps(out)


def test_the_links_of_a_skin_run_show_no_id_the_evidence_does_not_show():
    """An id is not a person: ids stay as they are so claims still resolve. A comment id a vendor scraped that equals a
    handle is shown by the evidence list already; the links must add nothing to it."""
    out = skins.mask_people(skin_record(MENTIONED), [], [])
    shown = {e["id"] for e in out["answer"]["evidence"]}
    links = out["evidence_links"]["parents"]
    assert set(links) <= shown and set(links.values()) <= shown


def test_the_skin_view_then_the_privacy_projection_names_nobody_and_drops_a_comment_of_a_hidden_post(monkeypatch):
    store = PrivStore(hide={"c_hid"})
    monkeypatch.setattr("core.api.store.get_store", lambda: store)
    monkeypatch.setattr(agent_app, "skin_people", lambda sid, rec=None: {"skin_id": sid, "approved": [], "allowed": []})
    record = skin_record()
    record["answer"]["evidence"][0]["id"] = "obs1_" + "a" * 32  # P_HID1, a post of the hidden creator c_hid
    record["answer"]["evidence"][1]["url"] = "https://www.tiktok.com/@somebody/video/1"
    record["evidence_links"] = {"v": 1, "parents": {COMMENT: "obs1_" + "a" * 32}}
    out = agent_app.readable(record)
    assert named(out) == []
    assert COMMENT not in {e["id"] for e in out["answer"]["evidence"]}
    assert "evidence_links" not in out
