"""Ask power review, Critical 1: a transcript span of a hidden person's post leaves with the post.

A span's id is the post's id plus _span_N, so it missed the creator lookup that matches stored post ids and survived
when the person was hidden by creator id and their handle no longer matched (a rename, or a creators row with no
handle). The words, link and old handle then stayed in the stored answer and in the live stream."""
import copy

import pytest

from core.api import privacy
from core.api.tests.test_privacy_projection import (P_HID1, P_NULL, P_REN1, P_VIS1, PrivStore, ask_record, body_of, ev)

SPAN_WORDS = "spoken words of the renamed creator"


def span(post_id, handle, n=1, text=SPAN_WORDS, posted="2026-10-05T01:00:00+02:00"):
    record = ev(f"{post_id}_span_{n}", handle, text, posted)
    record.update(parent_id=post_id, transcript_span={"start_s": 0.0, "end_s": 4.0, "text": text})
    return record


def hidden_of(store):
    return privacy.read_hidden(store)


def record_with_span(post_id=P_REN1, handle="hid_old_handle"):
    """A record whose only claim cites the span, as a claim of a transcript quote does."""
    out = ask_record()
    sp = span(post_id, handle)
    out["answer"]["evidence"] = [ev(P_VIS1, "vis_handle", "visible fixture words"), sp]
    out["answer"]["claims"] = [
        {"id": "c1", "text": "Amapiano posts rose", "label": "observed", "kind": "observation",
         "evidence_ids": [P_VIS1], "quotes": []},
        {"id": "c2", "text": "A creator said it aloud", "label": "single_source", "kind": "observation",
         "evidence_ids": [sp["id"]], "quotes": [{"evidence_id": sp["id"], "text": SPAN_WORDS}]}]
    out["answer"]["so_what"] = [{"text": "s1", "claim_ids": ["c2"]}, {"text": "s2", "claim_ids": ["c1"]}]
    out["answer"]["watch_next"] = []
    out["run"]["ranked_list"] = None
    return out


def test_the_span_of_a_hidden_creators_post_is_gone_with_the_post():
    store = PrivStore(hide={"c_ren"})
    records = [ev(P_REN1, "hid_old_handle", "post words", "2026-10-05T01:00:00+02:00"), span(P_REN1, "hid_old_handle")]
    assert privacy.evidence_gone(records, hidden_of(store), store) == {P_REN1, f"{P_REN1}_span_1"}


def test_the_span_of_a_visible_post_stays():
    store = PrivStore(hide={"c_ren"})
    records = [ev(P_VIS1, "vis_handle", "visible"), span(P_VIS1, "vis_handle", posted="2026-10-05T09:00:00+02:00")]
    assert privacy.evidence_gone(records, hidden_of(store), store) == set()


def test_a_span_whose_post_cannot_be_placed_is_gone():
    store = PrivStore(hide={"c_ren"})
    post_id = "obs1_" + "d" * 32  # in no posts table
    got = privacy.evidence_gone([span(post_id, "someone", posted="2026-10-05T09:00:00+02:00")], hidden_of(store), store)
    assert got == {f"{post_id}_span_1"}


def test_a_span_is_judged_as_its_post_is_when_the_post_has_no_creator():
    store = PrivStore(hide={"c_ren"})
    post = ev(P_NULL, "someone", "words", "2026-10-05T09:00:00+02:00")
    one = privacy.evidence_gone([post], hidden_of(store), store)
    both = privacy.evidence_gone([post, span(P_NULL, "someone", posted="2026-10-05T09:00:00+02:00")], hidden_of(store),
                                 store)
    assert both == one | ({f"{P_NULL}_span_1"} if P_NULL in one else set())


def test_a_span_alone_is_judged_without_its_post_in_the_record():
    store = PrivStore(hide={"c_ren"})
    assert privacy.evidence_gone([span(P_REN1, "hid_old_handle")], hidden_of(store), store) == {f"{P_REN1}_span_1"}


def test_two_spans_and_their_post_cost_one_lookup_of_one_id():
    store = PrivStore(hide={"c_ren"})
    seen = []
    original = store.post_creators
    store.post_creators = lambda ids, a, b: (seen.append(list(ids)), original(ids, a, b))[1]
    records = [ev(P_REN1, "hid_old_handle", "post", "2026-10-05T01:00:00+02:00"), span(P_REN1, "hid_old_handle", 1),
               span(P_REN1, "hid_old_handle", 2)]
    assert len(privacy.evidence_gone(records, hidden_of(store), store)) == 3
    assert seen == [[P_REN1]]


def test_a_failed_lookup_hides_the_span_too():
    store = PrivStore(hide={"c_ren"}, fail=None)
    hidden = hidden_of(store)
    store.fail = "lookup"
    with pytest.raises(privacy.PeopleUnavailable):
        privacy.evidence_gone([span(P_REN1, "hid_old_handle")], hidden, store)


def test_the_stored_answer_loses_the_claim_that_cited_only_the_span_end_to_end():
    store = PrivStore(hide={"c_ren"})
    out = privacy.project_record(record_with_span(), store)
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1"]
    assert [e["id"] for e in out["answer"]["evidence"]] == [P_VIS1]
    for leak in (SPAN_WORDS, "hid_old_handle", f"{P_REN1}_span_1"):
        assert leak not in body_of(out), leak


def test_the_same_record_with_a_visible_creator_keeps_the_span_claim():
    store = PrivStore(hide={"c_ren"})
    out = privacy.project_record(record_with_span(P_VIS1, "vis_handle"), store)
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1", "c2"]


def test_the_live_stream_withholds_the_span_and_the_claim_that_cites_it():
    store = PrivStore(hide={"c_ren"})
    hidden, gone, seen = hidden_of(store), set(), {}
    sp = span(P_REN1, "hid_old_handle")
    assert privacy.project_event("evidence", {"seq": 3, "evidence": sp}, hidden, store, gone, seen) is None
    claim = record_with_span()["answer"]["claims"][1]
    assert privacy.project_event("claim", {"seq": 4, "claim": claim}, hidden, store, gone, seen) is None
    visible = span(P_VIS1, "vis_handle", posted="2026-10-05T09:00:00+02:00")
    assert privacy.project_event("evidence", {"seq": 5, "evidence": visible}, hidden, store, gone, seen) is not None
