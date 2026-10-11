"""Release D ruling: a transcript span is a child of its post, as a comment is (parent_id travels).

Release C walked only a comment id to its post. These tests pin the four places that now follow a span too: the stored
link (comment_links.attach), the parent walk in privacy.evidence_gone, the live stream, and a record read after a hide.
A span of a post that is gone, or of a post nothing shows, is gone with it. Every leak string is a literal."""
import copy
import json

from core.agent.tests.test_ask import Harness, make_research
from core.agent.tests.test_span_children import OnlyTheSpan, SPAN_ID, SPAN_WORDS, SPOKEN, add_span
from core.api import agent_app, comment_links, privacy
from core.api.tests.test_privacy_projection import P_HID1, P_REN1, P_VIS1, PrivStore, ev, handle_row


def span(post_id, handle, n=1, text="spoken words of the creator", posted="2026-10-05T09:00:00+02:00", parent=None):
    record = ev(f"{post_id}_span_{n}", handle, text, posted)
    record.update(parent_id=parent or post_id, transcript_span={"start_s": 0.0, "end_s": 4.0, "text": text})
    return record


def hidden_of(store):
    return privacy.read_hidden(store)


# The stored link

def test_attach_gives_a_parent_to_a_span_record_and_to_a_comment_record_and_to_nothing_else():
    links = {"tt_p1_span_1": "tt_p1", "tiktok_comment_1": "tt_p1", "tt_p2": "tt_p1", "span_1": "tt_p1"}
    evidence = [{"id": "tt_p1"}, {"id": "tt_p1_span_1"}, {"id": "tiktok_comment_1"}, {"id": "tt_p2"}, {"id": "span_1"}]
    out = {e["id"]: e for e in comment_links.attach(evidence, links)}
    assert out["tt_p1_span_1"]["parent_id"] == "tt_p1" and out["tiktok_comment_1"]["parent_id"] == "tt_p1"
    assert all("parent_id" not in out[i] for i in ("tt_p1", "tt_p2", "span_1"))


def test_attach_leaves_a_span_that_already_holds_a_parent_alone():
    held = [{"id": "tt_p1_span_1", "parent_id": "tt_other"}]
    assert comment_links.attach(held, {"tt_p1_span_1": "tt_p1"}) == held


# The parent walk

def test_a_span_of_a_post_that_is_gone_is_gone_whatever_its_own_handle_says():
    store = PrivStore(hide={"c_hid"})
    post = ev(P_HID1, "hid_handle", "post words")
    child = span("tiktok_55", "vis_handle", parent=P_HID1)  # no creator lookup for this id, no hidden handle on it
    assert privacy.evidence_gone([post, child], hidden_of(store), store) == {P_HID1, "tiktok_55_span_1"}


def test_a_span_of_a_visible_post_stays_beside_the_post():
    store = PrivStore(hide={"c_hid"})
    records = [ev(P_VIS1, "vis_handle", "visible"), span(P_VIS1, "vis_handle")]
    assert privacy.evidence_gone(records, hidden_of(store), store) == set()


def test_a_span_whose_post_nothing_shows_is_gone_when_someone_is_hidden():
    store = PrivStore(hide={"c_hid"})
    assert privacy.evidence_gone([span(P_VIS1, "vis_handle")], hidden_of(store), store) == {f"{P_VIS1}_span_1"}


def test_a_span_is_not_gone_when_nobody_is_hidden():
    store = PrivStore(hide=set())
    assert privacy.evidence_gone([span(P_VIS1, "vis_handle")], hidden_of(store), store) == set()


def test_a_span_under_a_span_under_a_gone_post_is_gone():
    store = PrivStore(hide={"c_hid"})
    records = [ev(P_HID1, "hid_handle", "post words"), span("tiktok_55", "vis_handle", parent=P_HID1),
               span("tiktok_56", "vis_handle", parent="tiktok_55_span_1")]
    assert privacy.evidence_gone(records, hidden_of(store), store) >= {"tiktok_55_span_1", "tiktok_56_span_1"}


# The live stream

def test_the_live_stream_keeps_the_span_of_a_visible_post_that_it_sent_first():
    store = PrivStore(hide={"c_ren"})
    hidden, gone, seen = hidden_of(store), set(), {}
    sp = span(P_VIS1, "vis_handle")
    assert privacy.project_event("evidence", {"seq": 1, "evidence": ev(P_VIS1, "vis_handle", "visible")}, hidden, store, gone,
                                 seen) is not None
    assert privacy.project_event("evidence", {"seq": 2, "evidence": sp}, hidden, store, gone, seen) is not None
    assert gone == set()


def test_the_live_stream_withholds_the_span_of_a_post_it_never_sent():
    store = PrivStore(hide={"c_ren"})
    hidden, gone, seen = hidden_of(store), set(), {}
    sp = span(P_VIS1, "vis_handle")
    assert privacy.project_event("evidence", {"seq": 2, "evidence": sp}, hidden, store, gone, seen) is None
    assert gone == {sp["id"]}


def test_the_live_stream_keeps_the_span_beside_its_post_when_nobody_is_hidden_and_a_hide_lands_after():
    store = PrivStore(hide=set())
    hidden, gone, seen = hidden_of(store), set(), {}
    post, sp = ev(P_VIS1, "vis_handle", "visible"), span(P_VIS1, "vis_handle")
    assert privacy.project_event("evidence", {"seq": 1, "evidence": post}, hidden, store, gone, seen) is not None
    store.hide = {"c_ren"}
    hidden = hidden_of(store)
    assert privacy.project_event("evidence", {"seq": 2, "evidence": sp}, hidden, store, gone, seen) is not None


def test_the_stream_names_the_old_handle_of_a_hidden_creators_span_whose_post_it_never_sent():
    store = PrivStore(hide={"c_ren"})
    hidden, gone, seen, named = hidden_of(store), set(), {}, set()
    sp = span(P_REN1, "hid_old_handle", posted="2026-10-05T01:00:00+02:00")
    assert privacy.project_event("evidence", {"seq": 1, "evidence": sp}, hidden, store, gone, seen, named) is None
    assert "tiktok:hid_old_handle" in named


# The record a run stores, read after a hide

def research_with_a_span(ctx, prompt, options, emit, should_stop):
    out = make_research()(ctx, prompt, options, emit, should_stop)
    add_span(ctx)
    return out


def run_it():
    return Harness(check=None, research=research_with_a_span, model=OnlyTheSpan()).run()


def stored(result):
    ask = agent_app.Ask({"ask_id": "a_20260928_span", "question": "What is behind amapiano?", "market": "ZA"})
    agent_app.execute(ask, lambda request, emit, stop: result)
    return ask.record


def hide_handle(handle):
    store = PrivStore(hide=set())
    store.suppression_rows = [handle_row(handle=handle.lstrip("@"))]
    return store


def test_the_stored_record_holds_the_span_link_beside_the_answer():
    record = stored(run_it())
    assert record["evidence_links"] == {"v": 1, "parents": {SPAN_ID: "tt_1"}}
    assert comment_links.read(record) == {SPAN_ID: "tt_1"}
    assert {"tt_1", SPAN_ID} <= {e["id"] for e in record["answer"]["evidence"]}


def test_the_readers_drop_the_span_and_its_claim_when_the_creator_of_its_post_is_hidden():
    record = stored(run_it())
    handle = next(e["handle"] for e in record["answer"]["evidence"] if e["id"] == "tt_1")
    out = privacy.project_record(record, hide_handle(handle))
    shown = {e["id"] for e in out["answer"]["evidence"]}
    assert "tt_1" not in shown and SPAN_ID not in shown
    assert "c5" not in [c["id"] for c in out["answer"]["claims"]]
    assert SPAN_WORDS not in json.dumps(out) and SPOKEN not in json.dumps(out) and "evidence_links" not in out


def test_the_readers_keep_the_span_and_its_link_when_someone_else_is_hidden():
    record = stored(run_it())
    out = privacy.project_record(record, hide_handle("unrelated_account"))
    assert SPAN_ID in {e["id"] for e in out["answer"]["evidence"]}
    assert "c5" in [c["id"] for c in out["answer"]["claims"]]
    assert out["evidence_links"] == {"v": 1, "parents": {SPAN_ID: "tt_1"}}


def test_a_record_reads_the_same_with_the_span_link_or_without_it_when_someone_else_is_hidden():
    record = stored(run_it())
    bare = copy.deepcopy(record)
    del bare["evidence_links"]
    assert privacy.project_record(record, hide_handle("unrelated_account"))["answer"] == \
        privacy.project_record(bare, hide_handle("unrelated_account"))["answer"]


def test_a_record_with_a_span_and_no_hide_still_names_nobody():
    record = stored(run_it())
    assert privacy.project_record(copy.deepcopy(record), PrivStore(hide=set()))["answer"] == record["answer"]
