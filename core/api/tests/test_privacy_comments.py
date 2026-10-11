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


# What a stream remembers of an earlier event is what decides a later comment.
def test_a_stream_judges_a_comment_by_the_handle_of_its_post_alone():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    parent = {"seq": 3, "evidence": ev("tiktok_555", "hid_handle", "a live result")}
    assert privacy.project_event("evidence", parent, hidden, store, gone, seen) is None
    event = comment_event(C_HID, "tiktok_555", url="https://www.tiktok.com/@vis_handle/video/1")
    assert privacy.project_event("evidence", event, hidden, store, gone, seen) is None


def test_a_stream_judges_a_comment_by_the_creator_id_of_its_post_alone():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    parent = {"seq": 3, "evidence": {**ev("tiktok_556", "someone_else", "a live result"), "creator_id": "c_hid"}}
    assert privacy.project_event("evidence", parent, hidden, store, gone, seen) is None
    event = comment_event(C_HID, "tiktok_556", url="https://www.tiktok.com/@vis_handle/video/1")
    assert privacy.project_event("evidence", event, hidden, store, gone, seen) is None


def test_a_post_sent_while_nobody_was_hidden_still_judges_its_comment_later():
    store = PrivStore(hide=set())
    gone, seen = set(), {}
    vis = {"seq": 3, "evidence": ev(P_VIS1, "vis_handle", "visible fixture words")}
    ren = parent_event()
    assert privacy.project_event("evidence", vis, privacy.read_hidden(store), store, gone, seen) == vis
    assert privacy.project_event("evidence", ren, privacy.read_hidden(store), store, gone, seen) == ren
    store.hide.add("c_ren")  # a hide arrives after both posts went out
    hidden = privacy.read_hidden(store)
    kept = comment_event(C_VIS, P_VIS1, url="https://www.tiktok.com/@vis_handle/video/1")
    assert privacy.project_event("evidence", kept, hidden, store, gone, seen) == kept
    assert privacy.project_event("evidence", comment_event(), hidden, store, gone, seen) is None


def chain():
    """A post by a hidden creator, a comment under it by a visible author, and a reply under that comment."""
    post = ev(P_HID1, "hid_handle", "fixture hidden words one")
    first = comment(C_VIS, P_HID1, url="https://www.tiktok.com/@hid_handle/video/2")
    reply = comment(C_ORPHAN, C_VIS, handle="other_fan", url="https://www.tiktok.com/@hid_handle/video/2")
    return post, first, reply


def test_a_reply_under_a_comment_under_a_hidden_post_is_dropped_in_either_order():
    post, first, reply = chain()
    for order in ([post, first, reply], [reply, first, post]):
        store = PrivStore(hide={"c_hid"})
        gone = privacy.evidence_gone(order, privacy.read_hidden(store), store)
        assert gone == {P_HID1, C_VIS, C_ORPHAN}


def test_a_stream_drops_a_reply_whose_comment_it_sent_nothing_of():
    post, first, reply = chain()
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    for n, record in enumerate((post, first, reply), 3):
        assert privacy.project_event("evidence", {"seq": n, "evidence": record}, hidden, store, gone, seen) is None
    assert gone == {P_HID1, C_VIS, C_ORPHAN}


# The shape production stores: the comment is built by the tool, then kept by _output_record, which keeps parent_id and
# drops "via". An answer stored before parent_id was kept holds the same comment without it.
def built(parent_id=P_REN1, handle="hid_old_handle", legacy=False):
    from core.agent import checks
    from core.agent.tools.enrich_tools import _comment_record
    post = ev(parent_id, handle, "renamed creator words", "2026-10-05T01:00:00+02:00")
    item = {"comment_id": "77", "text": WORDS, "author": "fan_one", "created_at": "2026-10-05T10:00:00+02:00"}
    stored = checks._output_record(_comment_record(item, post, "tiktok", "tiktok/post/comments"))
    if legacy:
        stored.pop("parent_id", None)
    return checks._output_record(post), stored


def stored_record(legacy=False):
    post, remark = built(legacy=legacy)
    record = ask_record()
    record["answer"]["evidence"] = [e for e in record["answer"]["evidence"] if e["id"] != P_REN1] + [post, remark]
    record["answer"]["claims"].append(only_comment_claim(remark["id"]))
    return record, remark


@pytest.mark.parametrize("legacy", [False, True])
def test_a_stored_comment_with_no_url_of_its_own_leaks_nothing_on_every_surface(api, legacy):
    record, remark = stored_record(legacy)
    assert "hid_old_handle" in remark["url"]  # it took its post's address, old handle and all
    assert ("parent_id" in remark) is (not legacy)
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = record
    client = api.serve(remote_agent({}))
    page = client.get(f"/api/ask/{ASK}/export", headers=GOOD)
    body = client.get(f"/api/ask/{ASK}", headers=GOOD)
    assert page.status_code == 200 and body.status_code == 200
    for s in ("hid_old_handle", WORDS, remark["id"], "A commenter reacted"):
        assert s not in page.text and s not in body_of(body.json())
    store = api.store
    hidden = privacy.read_hidden(store)
    claim = only_comment_claim(remark["id"])
    assert privacy.selectable(claim, record["answer"]["evidence"], hidden, store) is False
    shown = privacy.project_dossier({"claims": [claim], "summary": "", "evidence": record["answer"]["evidence"]}, store)
    assert "hid_old_handle" not in body_of(shown) and WORDS not in body_of(shown)


def test_a_stored_comment_under_a_visible_post_is_kept_with_its_claim():
    post, remark = built(parent_id=P_VIS1, handle="vis_handle")
    record = ask_record()
    record["answer"]["evidence"] += [post, remark]
    record["answer"]["claims"].append(only_comment_claim(remark["id"]))
    out = project(record, PrivStore(hide={"c_hid"}))
    assert remark["id"] in ids_of(out) and "c9" in claim_ids(out)


def without_parent(c):
    return {k: v for k, v in c.items() if k != "parent_id"}


def test_a_legacy_comment_is_dropped_with_the_post_that_carries_its_address():
    out = project(with_comments(without_parent(comment(C_REN, None))), renamed_store())
    assert C_REN not in ids_of(out) and P_REN1 not in ids_of(out) and "hid_old_handle" not in body_of(out)


def test_a_legacy_comment_whose_address_names_a_hidden_author_is_dropped_though_its_post_is_visible():
    url = "https://www.tiktok.com/@hid_handle/video/9"
    record = ask_record()
    record["answer"]["evidence"] = [{**ev(P_VIS1, "vis_handle", "visible fixture words"), "url": url},
                                    without_parent(comment(C_HID, None, url=url))]
    out = project(record, PrivStore(hide={"c_hid"}))
    assert ids_of(out) == [P_VIS1]


def test_a_legacy_comment_whose_post_cannot_be_found_is_dropped():
    url = "https://www.tiktok.com/@elsewhere/video/5"
    out = project(with_comments(without_parent(comment(C_ORPHAN, None, url=url))), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN not in ids_of(out)


@pytest.mark.parametrize("url", [None, "", 7])
def test_a_legacy_comment_with_no_address_is_dropped(url):
    out = project(with_comments(without_parent(comment(C_ORPHAN, None, url=url))), PrivStore(hide={"c_hid"}))
    assert C_ORPHAN not in ids_of(out)


def test_a_legacy_comment_under_a_visible_post_that_carries_its_address_is_kept():
    url = "https://www.tiktok.com/@vis_handle/video/1"  # the address ev(P_VIS1, "vis_handle", ...) carries
    out = project(with_comments(without_parent(comment(C_VIS, None, url=url))), PrivStore(hide={"c_hid"}))
    assert C_VIS in ids_of(out)


def test_a_legacy_comment_is_untouched_while_nobody_is_hidden():
    record = with_comments(without_parent(comment(C_ORPHAN, None, url="https://www.tiktok.com/@elsewhere/video/5")))
    assert project(record, PrivStore(hide=set())) == record


@pytest.mark.parametrize("url,expected", [
    ("https://www.tiktok.com/@some.one/video/1", "@some.one"),
    ("https://x.com/somebody/status/1", "somebody"),
    ("https://twitter.com/somebody/status/1", "somebody"),
    ("https://www.reddit.com/user/somebody/comments/1", "u/somebody"),
    ("https://www.youtube.com/watch?v=1", None), (None, None), (7, None), ("http://[bad", None)])
def test_the_author_in_an_address_is_read_for_the_platforms_that_name_one(url, expected):
    assert privacy._url_handle(url) == expected


def test_a_stream_judges_a_legacy_comment_by_the_address_of_the_post_it_saw():
    store = renamed_store()
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    assert privacy.project_event("evidence", parent_event(), hidden, store, gone, seen) is None
    assert privacy.project_event("evidence", {"seq": 4, "evidence": without_parent(comment(C_REN, None))},
                                 hidden, store, gone, seen) is None


# The walk ends on a loop and does not give up on a long chain.
def ends(call, seconds=5):
    import threading
    done, box = threading.Event(), []

    def work():
        box.append(call())
        done.set()
    threading.Thread(target=work, daemon=True).start()
    assert done.wait(seconds), "the walk did not end"
    return box[0]


def loop_of(n):
    ids = [f"tiktok_comment_{i}" for i in range(n)]
    url = "https://www.tiktok.com/@vis_handle/video/1"
    return [comment(ids[i], ids[(i + 1) % n], url=url) for i in range(n)], ids


@pytest.mark.parametrize("n", [1, 2, 3, 7])
def test_a_loop_of_comments_ends_and_is_dropped(n):
    records, ids = loop_of(n)
    store = PrivStore(hide={"c_hid"})
    assert ends(lambda: privacy.evidence_gone(records, privacy.read_hidden(store), store)) == set(ids)


def test_a_loop_above_the_first_comment_ends_and_is_dropped():
    records, ids = loop_of(3)
    tail = comment("tiktok_comment_tail", ids[0], url="https://www.tiktok.com/@vis_handle/video/1")
    store = PrivStore(hide={"c_hid"})
    assert ends(lambda: privacy.evidence_gone([tail, *records], privacy.read_hidden(store), store)) == {*ids, tail["id"]}


@pytest.mark.parametrize("hidden", [False, True])
def test_a_long_chain_of_comments_is_judged_by_its_post_whatever_its_length(hidden):
    url = "https://www.tiktok.com/@hid_handle/video/2" if hidden else "https://www.tiktok.com/@vis_handle/video/1"
    post = ev(P_HID1 if hidden else P_VIS1, "hid_handle" if hidden else "vis_handle", "words")
    chain = [comment("tiktok_comment_0", post["id"], handle="a", url=url)]
    chain += [comment(f"tiktok_comment_{i}", f"tiktok_comment_{i - 1}", handle="a", url=url) for i in range(1, 40)]
    store = PrivStore(hide={"c_hid"})
    gone = ends(lambda: privacy.evidence_gone([*reversed(chain), post], privacy.read_hidden(store), store))
    assert gone == ({post["id"], *(c["id"] for c in chain)} if hidden else set())


def test_a_dossier_made_from_a_claim_that_cites_only_a_comment_carries_its_post():
    from core.api import dossiers
    record, remark = stored_record()
    sel = {"keep": ["c9"], "title": "Reaction", "notes": {}}
    body = dossiers.build(record, sel, dossier_id="d_fixture1", version=1, created_at="2026-10-07T10:00:00+02:00",
                          source={"ask_id": record["ask_id"]})
    assert [e["id"] for e in body["evidence"]] == [P_REN1, remark["id"]]
