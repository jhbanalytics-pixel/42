"""A stored answer keeps the post a comment sits under (C5 v2 section 5.1): the evidence schema carries parent_id, and an
answer whose claim cites a comment also carries the comment's post, so the privacy check can judge the comment by it."""
import copy

from core.agent import checks
from core.agent.answer import validate_answer
from core.agent.checks import check_answer
from core.agent.tests.test_checks import STORED, WINDOW, FakeWarehouse, ROWS, make_ctx, make_draft, run
from core.agent.tools.enrich_tools import _comment_record

COMMENT_TEXT = "Beetroot next to chakalaka is a lifestyle"


def comment_of(parent, native="77"):
    item = {"comment_id": native, "text": COMMENT_TEXT, "author": "fan_one", "created_at": "2026-09-27T15:00:00+02:00",
            "likes": 3}
    return _comment_record(item, parent, "tiktok", "tiktok/post/comments")


def comment_ctx():
    ctx = make_ctx()
    comment = comment_of(ctx.evidence["tt_1"])
    ctx.evidence[comment["id"]] = comment
    return ctx, comment


def comment_draft(ctx, comment, cited=None):
    draft = make_draft(ctx)
    draft["claims"][0]["evidence_ids"] = ["tt_3", "x_1"]  # no other claim cites tt_1, so only the comment can bring it in
    draft["claims"][2]["evidence_ids"] = ["x_1"]
    draft["claims"][1]["evidence_ids"] = cited or [comment["id"]]
    draft["claims"][1]["text"] = "A TikTok commenter said beetroot next to chakalaka is a lifestyle."
    draft["claims"][1]["quotes"] = [{"evidence_id": comment["id"], "text": "Beetroot next to chakalaka is a lifestyle"}]
    draft["evidence"] = [copy.deepcopy(ctx.evidence[i]) for i in ("tt_1", comment["id"])]
    return draft


def test_a_stored_comment_keeps_the_id_of_its_post():
    ctx, comment = comment_ctx()
    stored = checks._output_record(comment)
    assert stored["parent_id"] == "tt_1" and "via" not in stored


def test_a_stored_post_has_no_parent_id():
    assert "parent_id" not in checks._output_record(STORED[0])


def test_the_evidence_schema_accepts_parent_id_and_nothing_else_new():
    ctx, comment = comment_ctx()
    answer, _ = run(comment_draft(ctx, comment), ctx, FakeWarehouse(ROWS))
    assert next(e for e in answer["evidence"] if e["id"] == comment["id"])["parent_id"] == "tt_1"
    answer["evidence"][0]["via"] = "tiktok/post/comments"
    assert validate_answer(answer) != []
    del answer["evidence"][0]["via"]
    answer["evidence"][0]["parent_id"] = ""
    assert validate_answer(answer) != []


def test_an_answer_that_cites_only_a_comment_carries_the_post_it_sits_under():
    ctx, comment = comment_ctx()
    answer, _ = run(comment_draft(ctx, comment), ctx, FakeWarehouse(ROWS))
    ids = [e["id"] for e in answer["evidence"]]
    assert comment["id"] in ids and "tt_1" in ids
    assert ids.index("tt_1") < ids.index(comment["id"])  # the post is read before its comment


def test_a_post_that_is_cited_and_carried_is_listed_once():
    ctx, comment = comment_ctx()
    answer, _ = run(comment_draft(ctx, comment, cited=["tt_1", comment["id"]]), ctx, FakeWarehouse(ROWS))
    ids = [e["id"] for e in answer["evidence"]]
    assert ids.count("tt_1") == 1 and ids.count(comment["id"]) == 1


def test_a_comment_whose_post_is_not_in_the_run_adds_nothing():
    ctx, comment = comment_ctx()
    draft = comment_draft(ctx, comment)
    del ctx.evidence["tt_1"]
    answer, _ = check_answer(draft, ctx, FakeWarehouse(ROWS), window=WINDOW, markets=["ZA"])
    assert comment["id"] in [e["id"] for e in answer["evidence"]] and "tt_1" not in [e["id"] for e in answer["evidence"]]
