"""A comment is judged by the post it sits under (C5 v2 section 5.1, the older gap found in the second power review).

A comment id is "<platform>_comment_<native>" with parent_id set, so the stored-post lookup never sees it, and when the
comment has no address of its own it carries the parent post's address, handle and all. Every leak string below is a
literal written here, never read back from the fixture it judges."""
import copy

import pytest

from core.api import privacy
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, api, remote_agent  # noqa: F401  (fixtures)
from core.api.tests.test_privacy_projection import (P_HID1, P_REN1, P_VIS1, PrivStore, ask_record, body_of, claim_ids,
                                                    ev, handle_row, project)

C_REN = "tiktok_comment_901"
C_HID = "tiktok_comment_902"
C_VIS = "tiktok_comment_903"
C_ORPHAN = "tiktok_comment_904"
WORDS = "words said under the renamed creators post"
OLD_URL = "https://www.tiktok.com/@hid_old_handle/video/1"


def comment(cid, parent, handle="fan_handle", text=WORDS, url=OLD_URL):
    """A comment record as _comment_record builds it: no creator_id, the parent's address when it has none."""
    return {"id": cid, "platform": "tiktok", "handle": handle, "url": url, "posted_at": "2026-10-05T10:00:00+02:00",
            "market": "ZA", "text": text, "engagement": {"likes": 2}, "flags": ["market_assumed"], "parent_id": parent,
            "via": "comments"}


def with_comments(*comments, claim=None):
    record = ask_record()
    record["answer"]["evidence"].extend(comments)
    if claim:
        record["answer"]["claims"].append(claim)
    return record


def only_comment_claim(cid):
    return {"id": "c9", "text": "A commenter reacted", "label": "single_source", "kind": "observation",
            "evidence_ids": [cid], "quotes": [{"evidence_id": cid, "text": WORDS}]}


def ids_of(record):
    return [e["id"] for e in record["answer"]["evidence"]]


def renamed_store():
    """The renamed-creator probe: the hidden keys hold only the new handle, so the old handle in the comment's
    address matches nothing by name."""
    store = PrivStore(hide={"c_ren"})
    hidden = privacy.read_hidden(store)
    assert hidden[0] == {"tiktok:hid_new_handle"} and hidden[1] == {"c_ren"}
    return store


def test_a_comment_under_a_renamed_hidden_creators_post_leaks_nothing_through_the_record():
    record = with_comments(comment(C_REN, P_REN1), claim=only_comment_claim(C_REN))
    out = project(record, renamed_store())
    assert P_REN1 not in ids_of(out) and C_REN not in ids_of(out)
    assert "c9" not in claim_ids(out)
    text = body_of(out)
    for s in ("hid_old_handle", WORDS, C_REN, P_REN1, "hid_new_handle"):
        assert s not in text
    assert out["privacy"]["withheld"]["posts"] == 2 and out["privacy"]["withheld"]["claims"] == 2  # c4 and c9


def test_a_comment_under_a_post_dropped_by_handle_is_dropped_with_it():
    out = project(with_comments(comment(C_HID, P_HID1, url="https://www.tiktok.com/@hid_handle/video/2")),
                  PrivStore(hide={"c_hid"}))
    assert C_HID not in ids_of(out) and P_HID1 not in ids_of(out)
    assert "hid_handle" not in body_of(out)


def test_a_comment_under_a_post_hidden_only_by_a_handle_row_is_dropped():
    store = PrivStore(hide=set())
    store.suppression_rows = [handle_row()]
    assert privacy.read_hidden(store)[1] == set()  # no creator id is hidden, so no post lookup runs
    out = project(with_comments(comment(C_HID, P_HID1, url="https://www.tiktok.com/@hid_handle/video/2")), store)
    assert C_HID not in ids_of(out) and "hid_handle" not in body_of(out)


def test_a_comment_whose_parent_is_not_among_the_evidence_is_dropped():
    out = project(with_comments(comment(C_ORPHAN, "obs1_" + "9" * 32)), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN not in ids_of(out) and WORDS not in body_of(out)


@pytest.mark.parametrize("parent", ["", 7, ["x"], {"a": 1}])
def test_a_comment_whose_parent_id_cannot_name_a_post_is_dropped(parent):
    out = project(with_comments(comment(C_ORPHAN, parent)), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN not in ids_of(out)


def test_a_record_with_no_parent_id_is_judged_as_before():
    out = project(with_comments(comment(C_ORPHAN, None)), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN in ids_of(out)


def test_a_comment_under_a_post_the_lookup_cannot_place_is_dropped():
    store = PrivStore(hide={"c_hid"}, posts={P_VIS1: ("c_vis", "2026-10-05")})
    out = project(with_comments(comment(C_REN, P_REN1)), store)
    assert C_REN not in ids_of(out)


def test_a_comment_under_a_visible_post_is_kept_and_so_is_its_claim():
    record = with_comments(comment(C_VIS, P_VIS1, url="https://www.tiktok.com/@vis_handle/video/1"),
                           claim=only_comment_claim(C_VIS))
    out = project(record, PrivStore(hide={"c_hid"}))
    assert C_VIS in ids_of(out) and "c9" in claim_ids(out)
    kept = next(e for e in out["answer"]["evidence"] if e["id"] == C_VIS)
    assert kept == next(e for e in record["answer"]["evidence"] if e["id"] == C_VIS)


def test_a_comment_by_a_hidden_author_is_dropped_though_its_post_is_visible():
    record = with_comments(comment(C_HID, P_VIS1, handle="hid_handle", url="https://www.tiktok.com/@vis_handle/video/1"),
                           claim=only_comment_claim(C_HID))
    out = project(record, PrivStore(hide={"c_hid"}))
    assert C_HID not in ids_of(out) and P_VIS1 in ids_of(out) and "c9" not in claim_ids(out)
    assert "hid_handle" not in body_of(out)


def test_a_comment_under_a_comment_that_is_dropped_is_dropped():
    inner = comment(C_HID, P_VIS1, handle="hid_handle", url="https://www.tiktok.com/@vis_handle/video/1")
    outer = comment(C_ORPHAN, C_HID, url="https://www.tiktok.com/@vis_handle/video/1")
    out = project(with_comments(inner, outer), PrivStore(hide={"c_hid"}))
    assert C_HID not in ids_of(out) and C_ORPHAN not in ids_of(out)


def test_a_comment_that_names_itself_as_its_parent_is_not_its_own_proof():
    out = project(with_comments(comment(C_ORPHAN, C_ORPHAN)), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN not in ids_of(out)


def test_an_unreadable_list_still_drops_every_comment():
    record = with_comments(comment(C_VIS, P_VIS1))
    out = project(record, PrivStore(fail="list"))
    assert out["answer"]["evidence"] == [] and out["privacy"]["state"] == "unavailable"


def test_nothing_hidden_changes_nothing_for_a_comment():
    record = with_comments(comment(C_ORPHAN, "obs1_" + "9" * 32))
    assert project(record, PrivStore(hide=set())) == record


def test_the_stored_record_is_not_changed_by_judging_a_comment():
    record = with_comments(comment(C_REN, P_REN1))
    before = copy.deepcopy(record)
    project(record, renamed_store())
    assert record == before


def test_evidence_gone_names_a_dropped_comment_and_no_parent_that_was_only_looked_up():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone = privacy.evidence_gone([comment(C_HID, P_HID1)], hidden, store, parents=[ev(P_HID1, "hid_handle", "x")])
    assert gone == {C_HID}


# A run in flight: the parent arrives as an earlier event, the comment as a later one.
def parent_event(post_id=P_REN1, handle="hid_old_handle"):
    return {"seq": 3, "evidence": ev(post_id, handle, "renamed creator words", "2026-10-05T01:00:00+02:00")}


def comment_event(cid=C_REN, parent=P_REN1, **kw):
    return {"seq": 4, "evidence": comment(cid, parent, **kw)}


def test_a_stream_sends_no_comment_under_a_hidden_creators_post_and_no_claim_citing_it():
    store = renamed_store()
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    assert privacy.project_event("evidence", parent_event(), hidden, store, gone, seen) is None
    assert privacy.project_event("evidence", comment_event(), hidden, store, gone, seen) is None
    assert C_REN in gone
    claim = {"seq": 5, "claim": only_comment_claim(C_REN)}
    assert privacy.project_event("claim", claim, hidden, store, gone, seen) is None


def test_a_stream_sends_a_comment_under_a_visible_post():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    vis = {"seq": 3, "evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}
    assert privacy.project_event("evidence", vis, hidden, store, gone, seen) == vis
    event = comment_event(C_VIS, P_VIS1, url="https://www.tiktok.com/@vis_handle/video/1")
    assert privacy.project_event("evidence", event, hidden, store, gone, seen) == event
    assert gone == set()


def test_a_stream_withholds_a_comment_whose_parent_event_it_never_saw():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    assert privacy.project_event("evidence", comment_event(C_VIS, P_VIS1), hidden, store, gone, seen) is None
    assert C_VIS in gone


def test_a_stream_judges_the_parent_by_the_list_read_now():
    store = PrivStore(hide=set())
    gone, seen = set(), {}
    vis = {"seq": 3, "evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}
    event = comment_event(C_VIS, P_VIS1, url="https://www.tiktok.com/@vis_handle/video/1")
    assert privacy.project_event("evidence", vis, privacy.read_hidden(store), store, gone, seen) == vis
    store.hide.add("c_vis")  # the parent's author is hidden between the two events
    assert privacy.project_event("evidence", event, privacy.read_hidden(store), store, gone, seen) is None


# Dossiers and the HTML export read the same projection.
def test_a_dossier_view_drops_a_comment_under_a_hidden_creators_post():
    view = {"claims": [], "summary": ""}
    view["evidence"] = [ev(P_REN1, "hid_old_handle", "renamed creator words", "2026-10-05T01:00:00+02:00"),
                        comment(C_REN, P_REN1)]
    out = privacy.project_dossier(view, renamed_store())
    assert out["evidence"] == [] and WORDS not in body_of(out) and "hid_old_handle" not in body_of(out)


def test_a_dossier_claim_resting_on_such_a_comment_is_not_selectable():
    record = with_comments(comment(C_REN, P_REN1), claim=only_comment_claim(C_REN))
    store = renamed_store()
    hidden = privacy.read_hidden(store)
    claim = next(c for c in record["answer"]["claims"] if c["id"] == "c9")
    assert privacy.selectable(claim, record["answer"]["evidence"], hidden, store) is False
    assert privacy.selectable_claims({"c9": claim}, record["answer"]["evidence"], hidden, store) == {"c9": False}


def test_the_html_export_of_a_record_with_such_a_comment_leaks_nothing(api):
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = with_comments(comment(C_REN, P_REN1), claim=only_comment_claim(C_REN))
    r = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 200 and privacy.EXPORT_NOTICE in r.text
    for s in ("hid_old_handle", WORDS, C_REN, P_REN1, "A commenter reacted"):
        assert s not in r.text
    assert "Amapiano posts rose" in r.text


def test_the_json_read_of_a_record_with_such_a_comment_leaks_nothing(api):
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = with_comments(comment(C_REN, P_REN1), claim=only_comment_claim(C_REN))
    body = api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD).json()
    for s in ("hid_old_handle", WORDS, C_REN, P_REN1, "A commenter reacted"):
        assert s not in body_of(body)
