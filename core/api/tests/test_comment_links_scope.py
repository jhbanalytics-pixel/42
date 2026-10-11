"""Release C ruling: the stored links name comments only. attach gives a parent_id to a comment record and to nothing
else, so a link in a stored record that names a transcript span, or any other record, is not followed."""
from core.api import comment_links

LINKS = {"tiktok_comment_1": "tt_p1", "span_1": "tt_p1", "tt_p2": "tt_p1"}
EVIDENCE = [{"id": "tt_p1"}, {"id": "tiktok_comment_1"}, {"id": "span_1"}, {"id": "tt_p2"},
            {"id": "instagram_comment_9"}]


def test_attach_gives_a_parent_to_a_comment_record_only():
    out = {e["id"]: e for e in comment_links.attach(EVIDENCE, LINKS)}
    assert out["tiktok_comment_1"]["parent_id"] == "tt_p1"
    assert "parent_id" not in out["span_1"] and "parent_id" not in out["tt_p2"] and "parent_id" not in out["tt_p1"]


def test_attach_still_leaves_a_comment_that_already_holds_a_parent_and_a_comment_with_no_link():
    held = [{"id": "tiktok_comment_1", "parent_id": "tt_other"}, {"id": "instagram_comment_9"}]
    out = comment_links.attach(held, LINKS)
    assert out == held


def test_attach_does_not_change_the_records_it_was_given():
    before = [dict(e) for e in EVIDENCE]
    comment_links.attach(EVIDENCE, LINKS)
    assert EVIDENCE == before
