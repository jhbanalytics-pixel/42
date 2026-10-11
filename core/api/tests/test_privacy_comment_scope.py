"""Release C ruling: the parent walk, the answer's links and the stream's judgement are scoped to comment records (ids of
the form "<platform>_comment_<native>"). A record that is not a comment but carries a parent_id, as a transcript span
does once ask-power merges, is judged on its own and never walked to a parent."""
from core.agent import checks
from core.api import privacy
from core.api.store import creator_key

HIDDEN = ({creator_key("tiktok", "hid_handle")}, set(), {})
POST = {"id": "tt_p1", "platform": "tiktok", "handle": "hid_handle", "url": "https://www.tiktok.com/@hid_handle/video/1"}
VISIBLE = {"id": "tt_p2", "platform": "tiktok", "handle": "vis_handle", "url": "https://www.tiktok.com/@vis_handle/video/2"}


def child(cid, parent, handle="vis_handle"):
    return {"id": cid, "platform": "tiktok", "handle": handle, "url": "https://www.tiktok.com/@vis_handle/video/2",
            "parent_id": parent}


def test_a_comment_under_a_hidden_post_is_gone_and_a_span_under_it_is_not_walked():
    comment, span = child("tiktok_comment_1", "tt_p1"), child("span_1", "tt_p1")
    assert privacy.evidence_gone([POST, VISIBLE, comment, span], HIDDEN, None) == {"tt_p1", "tiktok_comment_1"}


def test_a_span_whose_parent_cannot_be_found_is_not_gone_but_a_comment_is():
    comment, span = child("tiktok_comment_1", "tt_missing"), child("span_1", "tt_missing")
    assert privacy.evidence_gone([VISIBLE, comment, span], HIDDEN, None) == {"tiktok_comment_1"}


def test_the_walk_stops_at_a_post_that_carries_a_parent_id_itself():
    post = {**VISIBLE, "parent_id": "tt_p1"}  # not a comment, so its own parent_id is not followed
    comment = child("tiktok_comment_1", "tt_p2")
    assert privacy.evidence_gone([POST, post, comment], HIDDEN, None) == {"tt_p1"}


STORED = {"tiktok_comment_1": child("tiktok_comment_1", "tt_p2"), "span_1": child("span_1", "tt_p2"), "tt_p2": VISIBLE}


def test_a_cited_span_does_not_bring_its_parent_into_the_answer():
    assert checks._with_parents(["span_1"], STORED) == ["span_1"]
    assert checks._with_parents(["tiktok_comment_1"], STORED) == ["tt_p2", "tiktok_comment_1"]
    assert checks.cited_with_parents([{"evidence_ids": ["span_1"]}], STORED) == {"span_1"}


def test_the_answers_links_name_comments_only():
    answer = {"evidence": [{"id": "tt_p2"}, {"id": "span_1"}, {"id": "tiktok_comment_1"}]}
    assert checks.comment_links(answer, STORED) == {"v": 1, "parents": {"tiktok_comment_1": "tt_p2"}}
    assert checks.comment_links({"evidence": [{"id": "tt_p2"}, {"id": "span_1"}]}, STORED) is None
