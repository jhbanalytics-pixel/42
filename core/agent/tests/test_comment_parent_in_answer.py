"""A stored answer keeps the post a comment sits under (C5 v2 section 5.1): the evidence schema carries parent_id, and an
answer whose claim cites a comment also carries the comment's post, so the privacy check can judge the comment by it."""
import copy
import hashlib
import json
from pathlib import Path

import jsonschema

from core.agent import checks
from core.agent.answer import validate_answer
from core.agent.checks import check_answer
from core.agent.tests.test_checks import STORED, WINDOW, FakeWarehouse, ROWS, make_ctx, make_draft, run
from core.agent.tools.enrich_tools import _comment_record

RELEASE_A_EVIDENCE_KEYS = {"id", "platform", "handle", "url", "posted_at", "market", "source_market", "text", "engagement",
                           "flags", "thumbnail_url", "duration_s", "transcript_span", "creator_tier"}
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


RELEASE_A_SCHEMA_SHA256 = "ccf50a86acaeaa52aaac055a26ff4829a928211e7004e7d6ed32fbd61242bc02"  # core/eval/answer.schema.json at 3710c5e


def test_the_answer_schema_is_the_release_a_file_byte_for_byte():
    """Release A is the rollback target: its readers must read every answer this code stores, so the shape a stored
    answer is judged by is pinned here. A change to the file fails this test, not a reader in the field."""
    path = Path(__file__).resolve().parents[2] / "eval" / "answer.schema.json"
    assert hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == RELEASE_A_SCHEMA_SHA256


def test_a_stored_comment_has_no_parent_id_for_a_release_a_reader_to_refuse():
    ctx, comment = comment_ctx()
    assert comment["parent_id"] == "tt_1"  # the in-run record has it
    assert set(checks._output_record(comment)) <= RELEASE_A_EVIDENCE_KEYS


def test_an_answer_that_cites_a_comment_passes_the_release_a_schema_unchanged():
    ctx, comment = comment_ctx()
    answer, _ = run(comment_draft(ctx, comment), ctx, FakeWarehouse(ROWS))  # run() asserts validate_answer == []
    schema = json.loads((Path(__file__).resolve().parents[2] / "eval" / "answer.schema.json").read_text(encoding="utf-8"))
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(answer))
    assert errors == []
    kept = {e["id"] for e in answer["evidence"]}
    assert comment["id"] in kept and "tt_1" in kept
    assert all(set(e) <= RELEASE_A_EVIDENCE_KEYS for e in answer["evidence"])


def test_the_link_from_a_comment_to_its_post_is_kept_beside_the_answer():
    ctx, comment = comment_ctx()
    answer, _ = run(comment_draft(ctx, comment), ctx, FakeWarehouse(ROWS))
    assert checks.comment_links(answer, ctx.evidence) == {"v": 1, "parents": {comment["id"]: "tt_1"}}


def test_no_link_is_kept_for_an_answer_with_no_comment():
    ctx = make_ctx()
    answer, _ = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert checks.comment_links(answer, ctx.evidence) is None


def test_a_link_to_a_post_the_answer_does_not_carry_is_not_kept():
    answer = {"evidence": [{"id": "tiktok_comment_1"}]}
    stored = {"tiktok_comment_1": {"id": "tiktok_comment_1", "parent_id": "tt_gone"}}
    assert checks.comment_links(answer, stored) is None


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
