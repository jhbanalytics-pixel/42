"""Current suppression on stored and held copies (C5 v2 sections 5 and 6; matrix rows A14, A33 to A37, A42 to A44).

Every leak string below is a literal written here, never read back from the fixture it judges."""
import copy
import json

import pytest

from core.api import dossiers, privacy
from core.api.store import FixtureStore

LEAK = ["hid_handle", "@hid_handle", "c_hid", "fixture hidden words one", "fixture hidden words two",
        "https://www.tiktok.com/@hid_handle/video/2", "Hidden Fixture Name"]
P_VIS1 = "obs1_" + "1" * 32
P_HID1 = "obs1_" + "a" * 32
P_REN1 = "obs1_" + "c" * 32
P_NULL = "obs1_" + "e" * 32
HIDDEN_POST_LEAKS = [P_HID1]
CREATORS = {
    "c_vis": {"creator_id": "c_vis", "platform": "tiktok", "handle": "vis_handle"},
    "c_hid": {"creator_id": "c_hid", "platform": "tiktok", "handle": "hid_handle"},
    "c_hid2": {"creator_id": "c_hid2", "platform": "x", "handle": "hid2_handle"},
    "c_ren": {"creator_id": "c_ren", "platform": "tiktok", "handle": "hid_new_handle"},
}
# post_id: (creator_id, post_date). p_ren_1's date is the day before its posted_at, as a time-zone offset makes it.
POSTS = {P_VIS1: ("c_vis", "2026-10-05"), P_HID1: ("c_hid", "2026-10-05"), P_REN1: ("c_ren", "2026-10-04"),
         P_NULL: (None, "2026-10-05")}


class PrivStore(FixtureStore):
    """A store with literal creators and posts and a hide list the test controls. fail is "list" (the view raises),
    "missing" (the view does not exist) or "lookup" (the posts lookup raises)."""

    def __init__(self, hide=(), fail=None, posts=None):
        super().__init__()
        self.hide, self.fail, self.posts = set(hide), fail, dict(POSTS if posts is None else posts)
        self.calls, self.writes = [], []

    def suppressed_creators(self):
        self.calls.append("suppressed_creators")
        if self.fail == "list":
            raise RuntimeError("view unreadable")
        return None if self.fail == "missing" else set(self.hide)

    def creators_by_id(self, creator_ids):
        self.calls.append("creators_by_id")
        return [dict(CREATORS[i]) for i in sorted(creator_ids) if i in CREATORS]

    def map_items(self, item_ids):
        return []

    def post_creators(self, post_ids, since, until):
        self.calls.append("post_creators")
        self.last_window = (since, until)
        if self.fail == "lookup":
            raise RuntimeError("posts unreadable")
        return [{"post_id": p, "creator_id": self.posts[p][0]} for p in post_ids
                if p in self.posts and since <= self.posts[p][1] <= until]


def ev(post_id, handle, text, posted="2026-10-05T09:00:00+02:00", platform="tiktok"):
    return {"id": post_id, "platform": platform, "handle": handle, "url": f"https://www.tiktok.com/@{handle}/video/1",
            "posted_at": posted, "market": "ZA", "text": text}


def ask_record():
    """FX-ASK-ORD."""
    return {
        "ask_id": "a_20261007_aaaa0001", "question": "What is @hid_handle doing with amapiano in South Africa?",
        "market": "ZA", "status": "complete", "created_at": "2026-10-07T09:00:00+02:00",
        "finished_at": "2026-10-07T09:02:00+02:00", "error": None, "parent_id": None,
        "steps": [{"seq": 1, "text": "Searching TikTok for amapiano from @hid_handle"},
                  {"seq": 2, "text": "Reading 3 posts"}],
        "run": {"posts": 3, "ranked_list": {"platform": "tiktok", "items": [
            {"claim_id": "c1", "usage_handle": "vis_handle", "tied_with_previous": False},
            {"claim_id": "c2", "usage_handle": "hid_handle", "tied_with_previous": True},
            {"claim_id": "c4", "usage_handle": None, "tied_with_previous": False}]}},
        "answer": {
            "status": "complete", "as_of": "2026-10-07T09:00:00+02:00",
            "short_answer": "@hid_handle leads amapiano this week.",
            "evidence": [ev(P_VIS1, "vis_handle", "visible fixture words"),
                         ev(P_HID1, "hid_handle", "fixture hidden words one"),
                         ev(P_REN1, "hid_old_handle", "renamed creator words", "2026-10-05T01:00:00+02:00")],
            "claims": [
                {"id": "c1", "text": "Amapiano posts rose", "label": "observed", "kind": "observation",
                 "evidence_ids": [P_VIS1], "quotes": [{"evidence_id": P_VIS1, "text": "visible fixture words"}],
                 "numbers": [{"value": 3, "unit": "posts", "query_id": "q_1", "run_id": "r_1",
                              "result_hash": "sha256:" + "0" * 64}]},
                {"id": "c2", "text": "The hidden creator led", "label": "single_source", "kind": "observation",
                 "evidence_ids": [P_HID1], "quotes": [{"evidence_id": P_HID1, "text": "fixture hidden words one"}]},
                {"id": "c3", "text": "Both posted", "label": "corroborated", "kind": "observation",
                 "evidence_ids": [P_VIS1, P_HID1]},
                {"id": "c4", "text": "A renamed creator posted", "label": "single_source", "kind": "observation",
                 "evidence_ids": [P_REN1]}],
            "so_what": [{"text": "s1", "claim_ids": ["c2"]}, {"text": "s2", "claim_ids": ["c1"]},
                        {"text": "s3", "claim_ids": ["c1", "c3"]}],
            "watch_next": [{"text": "w1", "claim_ids": ["c4"], "forecast": None}],
            "gaps": []}}


def ask_all_hidden():
    record = ask_record()
    for claim in record["answer"]["claims"]:
        claim["evidence_ids"], claim["quotes"] = [P_HID1], []
    return record


def body_of(value):
    return json.dumps(value, ensure_ascii=False)


def leaks(value, extra=()):
    text = body_of(value)
    return [s for s in [*LEAK, *extra] if s in text]


def project(record, store, **kw):
    return privacy.project_record(record, store, **kw)


def claim_ids(record):
    return [c["id"] for c in record["answer"]["claims"]]


def test_a_hidden_creator_leaves_a_record_stored_before_the_hide_a14():
    store = PrivStore(hide={"c_hid"})
    record = ask_record()
    before = copy.deepcopy(record)
    out = project(record, store)
    assert [e["id"] for e in out["answer"]["evidence"]] == [P_VIS1, P_REN1]
    assert claim_ids(out) == ["c1", "c4"]
    assert [i["text"] for i in out["answer"]["so_what"]] == ["s2"]
    assert [i["text"] for i in out["answer"]["watch_next"]] == ["w1"]
    assert out["answer"]["short_answer"] == privacy.SUMMARY_WITHHELD
    assert len(out["answer"]["gaps"]) == 1 and out["answer"]["gaps"][0]["why"] == "withheld_privacy"
    assert out["answer"]["status"] == "complete"
    assert out["privacy"] == {"v": 1, "state": "applied", "withheld": {"posts": 1, "claims": 2, "summary": True}}
    assert leaks(out, HIDDEN_POST_LEAKS) == []
    assert out["answer"]["claims"][0] == before["answer"]["claims"][0]  # the control is intact
    assert out["answer"]["evidence"][0] == before["answer"]["evidence"][0]
    assert record == before  # the stored copy is not touched


def test_a_creator_renamed_since_the_record_was_made_is_found_by_post_id_a43a():
    out = project(ask_record(), PrivStore(hide={"c_ren"}))
    assert [e["id"] for e in out["answer"]["evidence"]] == [P_VIS1, P_HID1]
    assert claim_ids(out) == ["c1", "c2", "c3"]
    assert out["privacy"]["withheld"] == {"posts": 1, "claims": 1, "summary": True}
    assert "hid_old_handle" not in body_of(out)


def test_a_record_whose_every_claim_rests_on_a_hidden_post_becomes_a_partial_answer_with_no_claims():
    out = project(ask_all_hidden(), PrivStore(hide={"c_hid"}))
    assert out["answer"]["claims"] == [] and out["answer"]["status"] == "partial"
    assert out["answer"]["so_what"] == [] and out["answer"]["watch_next"] == []
    assert leaks(out, HIDDEN_POST_LEAKS) == []


def test_a_summary_that_was_already_blank_stays_blank():
    record = ask_record()
    record["answer"]["short_answer"] = ""
    out = project(record, PrivStore(hide={"c_hid"}))
    assert out["answer"]["short_answer"] == ""
    assert out["privacy"]["withheld"]["summary"] is False


def test_the_projection_reads_and_writes_nothing_beyond_the_list_and_one_post_lookup_a35_a44():
    store = PrivStore(hide={"c_hid"})
    project(ask_record(), store)
    assert store.calls == ["suppressed_creators", "creators_by_id", "post_creators"]
    assert store.writes == []


def test_a_hide_added_between_two_reads_shows_on_the_second_read_a44():
    store = PrivStore(hide={"c_hid"})
    record = ask_record()
    record["answer"]["evidence"].append(ev(P_NULL, "hid2_handle", "fixture hidden words two", platform="x"))
    first = project(record, store)
    store.hide.add("c_hid2")
    second = project(record, store)
    assert "hid2_handle" in body_of(first) or "fixture hidden words two" in body_of(first)
    assert "hid2_handle" not in body_of(second) and "fixture hidden words two" not in body_of(second)
    assert store.calls.count("suppressed_creators") == 2  # the list is read on every read


def test_a_lifted_suppression_returns_the_stored_copy_with_no_marker_a37():
    record = ask_record()
    out = project(record, PrivStore(hide=set()))
    assert out == record and "privacy" not in out


@pytest.mark.parametrize("fail", ["list", "missing", "lookup"])
def test_when_the_list_cannot_be_read_no_post_and_no_claim_is_shown_u(fail):
    store = PrivStore(hide={"c_hid"}, fail=fail)
    out = project(ask_record(), store)
    assert out["answer"]["evidence"] == [] and out["answer"]["claims"] == []
    assert out["answer"]["so_what"] == [] and out["answer"]["watch_next"] == []
    assert out["answer"]["short_answer"] == privacy.SUMMARY_WITHHELD
    assert out["answer"]["status"] == "partial"
    assert out["privacy"]["state"] == "unavailable" and out["privacy"]["note"] == privacy.UNAVAILABLE_NOTE
    assert leaks(out, HIDDEN_POST_LEAKS) == []
    assert "ranked_list" not in out["run"]


def test_an_unavailable_list_still_masks_every_handle_the_shape_layers_can_see():
    out = project(ask_record(), PrivStore(fail="list"))
    assert "@hid_handle" not in body_of(out) and "https://www.tiktok.com/@hid_handle" not in body_of(out)


def test_a_marker_in_the_stored_record_is_discarded_and_the_real_counts_are_set_a33():
    forged = ask_record()
    forged["privacy"] = {"v": 1, "state": "applied", "withheld": {"posts": 0, "claims": 0, "summary": False}}
    out = project(forged, PrivStore(hide={"c_hid"}))
    assert out["privacy"]["withheld"] == {"posts": 1, "claims": 2, "summary": True}
    clean = project(forged, PrivStore(hide=set()))
    assert "privacy" not in clean


def test_projecting_a_projection_changes_nothing_and_the_counts_do_not_double_a36():
    store = PrivStore(hide={"c_hid"})
    first = project(ask_record(), store)
    second = project(first, store, inbound=first["privacy"])
    assert second == first
    again = project(first, store)  # without the agent's marker the second pass has nothing to add
    assert {k: v for k, v in again.items() if k != "privacy"} == {k: v for k, v in first.items() if k != "privacy"}


def test_the_agents_marker_and_this_passes_own_marker_are_merged_not_trusted_r7():
    agent = {"v": 1, "state": "applied", "withheld": {"posts": 2, "claims": 1, "summary": False}}
    out = project(ask_record(), PrivStore(hide={"c_hid"}), inbound=agent)
    assert out["privacy"]["withheld"] == {"posts": 3, "claims": 3, "summary": True}
    junk = project(ask_record(), PrivStore(hide=set()), inbound={"v": 1, "state": "applied", "withheld": "many"})
    assert "privacy" not in junk


def test_the_typed_summary_state_is_not_read_or_rewritten_by_the_projection():
    record = ask_record()
    record["answer_meta"] = {"state": "verified", "words": "fixture hidden words one @hid_handle"}
    out = project(record, PrivStore(hide={"c_hid"}))
    assert out["answer_meta"] == record["answer_meta"]


def test_the_ranked_list_loses_a_withheld_claim_its_handle_and_the_tie_that_pointed_at_it_a42():
    out = project(ask_record(), PrivStore(hide={"c_hid"}))
    items = out["run"]["ranked_list"]["items"]
    assert [i["claim_id"] for i in items] == ["c1", "c4"]
    assert items[1]["tied_with_previous"] is False
    assert "hid_handle" not in body_of(out)
    renamed = project(ask_record(), PrivStore(hide={"c_ren"}))
    assert [i["claim_id"] for i in renamed["run"]["ranked_list"]["items"]] == ["c1", "c2"]


def test_a_ranked_list_with_every_item_withheld_is_removed_and_unavailable_blanks_handles_a42():
    gone = project(ask_all_hidden(), PrivStore(hide={"c_hid"}))
    assert "ranked_list" not in gone["run"]
    record = ask_record()
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] in ("c1", "c4")]
    record["answer"]["so_what"], record["answer"]["watch_next"] = [], []
    only_visible = project(record, PrivStore(fail="missing"))
    assert "ranked_list" not in only_visible["run"]


def test_a_stored_post_the_lookup_cannot_place_is_withheld_and_counted_a43b():
    store = PrivStore(hide={"c_hid"}, posts={P_VIS1: ("c_vis", "2026-10-05")})
    out = project(ask_record(), store)
    assert [e["id"] for e in out["answer"]["evidence"]] == [P_VIS1]
    assert claim_ids(out) == ["c1"]
    assert out["privacy"]["withheld"]["posts"] == 2


def test_a_live_post_id_is_kept_unless_its_handle_is_hidden_a43c():
    record = ask_record()
    record["answer"]["evidence"].append(ev("tiktok_123", "vis_handle", "a live result"))
    record["answer"]["evidence"].append(ev("tiktok_456", "hid_handle", "a hidden live result"))
    out = project(record, PrivStore(hide={"c_hid"}))
    ids = [e["id"] for e in out["answer"]["evidence"]]
    assert "tiktok_123" in ids and "tiktok_456" not in ids


def test_the_lookup_is_bounded_by_the_records_own_dates_with_three_days_either_side_a43e_a43f():
    store = PrivStore(hide={"c_hid"})
    project(ask_record(), store)
    assert store.last_window == ("2026-10-02", "2026-10-08")
    # posted_at 2026-10-05 and post_date 2026-10-04: one day off, still found
    assert P_REN1 in {e["id"] for e in project(ask_record(), store)["answer"]["evidence"]}
    far = PrivStore(hide={"c_hid"}, posts={**POSTS, P_REN1: ("c_ren", "2026-10-09")})
    out = project(ask_record(), far)  # four days from posted_at: outside the window, so unplaced and withheld
    assert P_REN1 not in {e["id"] for e in out["answer"]["evidence"]}


def test_a_post_with_no_creator_is_kept_it_has_no_one_to_match_a43():
    record = ask_record()
    record["answer"]["evidence"].append(ev(P_NULL, "someone", "an unattributed post"))
    out = project(record, PrivStore(hide={"c_hid"}))
    assert P_NULL in {e["id"] for e in out["answer"]["evidence"]}


# Dossiers (5.3).
def dossier_view(hidden_title=True):
    record = ask_record()
    sel = {"keep": ["c1", "c2", "c3"], "title": "Amapiano and @hid_handle" if hidden_title else "Amapiano",
           "notes": {"c1": "checked @hid_handle"}}
    body = dossiers.build(record, sel, dossier_id="d_fixture1", version=1, created_at="2026-10-07T10:00:00+02:00",
                          source={"ask_id": record["ask_id"]})
    ticks = {"c1": {"ticked": True, "note": "checked @hid_handle", "who": "passcode", "at": "2026-10-07T10:05:00+02:00"}}
    return {**body, "content_hash": dossiers.content_hash(body), "ticks": ticks, "needs_tick": []}, body


def test_a_dossier_keeps_its_slots_and_shows_a_withheld_claim_in_words_a25_a34():
    view, body = dossier_view()
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}))
    by = {c["claim_id"]: c for c in out["claims"]}
    assert list(by) == ["c1", "c2", "c3", "c4"] and by["c4"]["kept"] is False
    assert by["c1"]["text"] == "Amapiano posts rose" and by["c1"]["quotes"][0]["text"] == "visible fixture words"
    for cid in ("c2", "c3"):
        assert by[cid]["text"] == privacy.CLAIM_WITHHELD and by[cid]["withheld"] is True
        assert by[cid]["evidence_ids"] == [] and by[cid]["quotes"] == []
        assert by[cid]["label"] == body["claims"][int(cid[1]) - 1]["label"]  # the slot's facts stay
    assert [e["id"] for e in out["evidence"]] == [P_VIS1]
    assert out["summary"] is None or out["summary"] == privacy.SUMMARY_WITHHELD
    assert out["title"] == "Amapiano and ***" or "hid_handle" not in out["title"]
    assert out["ticks"]["c1"]["note"] != "checked @hid_handle" and "hid_handle" not in body_of(out["ticks"])
    assert "hid_handle" not in body_of(out) and P_HID1 not in body_of(out)
    assert out["privacy"] == {"v": 1, "state": "applied", "withheld": {"posts": 1, "claims": 2, "summary": False}}
    assert out["content_hash"] == dossiers.content_hash(body)
    assert out["content_hash"] != dossiers.content_hash({k: v for k, v in out.items() if k not in
                                                         ("content_hash", "ticks", "needs_tick", "privacy")})


def test_a_dossier_with_a_summary_over_every_claim_withholds_the_summary():
    view, _ = dossier_view()
    view["summary"] = "Hidden creator leads."
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}))
    assert out["summary"] == privacy.SUMMARY_WITHHELD and out["privacy"]["withheld"]["summary"] is True


def test_a_lifted_suppression_returns_the_dossier_as_stored_a37():
    view, _ = dossier_view()
    assert privacy.project_dossier(view, PrivStore(hide=set())) == view


def test_an_unreadable_list_withholds_every_dossier_claim_and_post():
    view, _ = dossier_view()
    out = privacy.project_dossier(view, PrivStore(fail="list"))
    assert all(c["withheld"] for c in out["claims"]) and out["evidence"] == []
    assert out["privacy"]["state"] == "unavailable"
    assert "@hid_handle" not in body_of(out)


def test_projecting_a_dossier_twice_changes_nothing_a36():
    view, _ = dossier_view()
    store = PrivStore(hide={"c_hid"})
    once = privacy.project_dossier(view, store)
    twice = privacy.project_dossier(once, store)
    assert {k: v for k, v in twice.items() if k != "privacy"} == {k: v for k, v in once.items() if k != "privacy"}


def test_a_dossier_list_row_has_its_title_and_question_masked_and_its_counts_left_a27():
    row = {"dossier_id": "d_fixture1", "version": 1, "state": "draft", "title": "Amapiano and @hid_handle",
           "question": "What is @hid_handle doing?", "kept": 3, "frozen_version": None}
    out = privacy.project_summary(row, PrivStore(hide={"c_hid"}))
    assert "hid_handle" not in body_of(out) and out["kept"] == 3 and out["frozen_version"] is None
    assert privacy.project_summary(row, PrivStore(hide=set())) == row


# What a dossier may keep (section 7, A41).
def test_a_claim_is_selectable_only_when_no_hidden_post_and_no_hidden_name_is_in_it_a41():
    record = ask_record()
    plain = {"id": "k2", "text": "Amapiano grew", "evidence_ids": [P_VIS1], "quotes": []}
    naming = {"id": "k1", "text": "Posts by @hid_handle grew", "evidence_ids": [P_VIS1], "quotes": []}
    evidence = record["answer"]["evidence"]
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    assert privacy.selectable(plain, evidence, hidden, store) is True
    assert privacy.selectable(naming, evidence, hidden, store) is False
    assert privacy.selectable(record["answer"]["claims"][1], evidence, hidden, store) is False
    assert privacy.selectable(plain, evidence, None, store) is False
    lifted = PrivStore(hide=set())
    assert privacy.selectable(naming, evidence, privacy.read_hidden(lifted), lifted) is True


# A run in flight (5.5, case G).
def test_a_stream_sends_no_hidden_evidence_or_claim_and_masks_its_steps_a10():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    hid = {"seq": 3, "evidence": ev(P_HID1, "hid_handle", "fixture hidden words one")}
    vis = {"seq": 4, "evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}
    claim = {"seq": 5, "claim": {"id": "c2", "text": "x", "evidence_ids": [P_HID1]}}
    step = {"seq": 1, "text": "Searching TikTok for amapiano from @hid_handle"}
    assert privacy.project_event("evidence", hid, hidden, store, gone, seen) is None
    assert privacy.project_event("evidence", vis, hidden, store, gone, seen) == vis
    assert privacy.project_event("claim", claim, hidden, store, gone, seen) is None
    assert "hid_handle" not in body_of(privacy.project_event("step", step, hidden, store, gone, seen))


def test_an_unreadable_list_sends_no_evidence_and_no_claim_event_a10():
    store = PrivStore(fail="list")
    vis = {"seq": 4, "evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}
    assert privacy.project_event("evidence", vis, None, store, set(), {}) is None
    assert privacy.project_event("claim", {"claim": {"id": "c1", "evidence_ids": [P_VIS1]}}, None, store, set(), {}) is None


def test_a_run_in_flight_may_use_a_list_no_older_than_thirty_seconds_case_g(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(privacy, "_clock", lambda: now[0])
    store = PrivStore(hide=set())
    fresh = privacy.FreshList(store)
    assert privacy.nothing_hidden(fresh.get())
    store.hide.add("c_hid")
    now[0] += privacy.SUPPRESSION_MAX_AGE_S - 1
    assert privacy.nothing_hidden(fresh.get())  # not required to show inside 30 seconds
    now[0] += 2
    assert not privacy.nothing_hidden(fresh.get()) and store.calls.count("suppressed_creators") == 2
