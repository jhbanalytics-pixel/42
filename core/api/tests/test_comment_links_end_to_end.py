"""A comment cited by a real run reaches the stored record with its link to the post, and the privacy readers use it.

The run is core.agent.ask.run_ask with the offline fakes of the agent tests, a comment built by the tool's own record
builder, and the real code checks (check=None). The result goes through agent_app.execute to the record the runs table
keeps, and the readers judge that record."""
import copy
import json

from core.agent.answer import validate_answer
from core.agent.tests.test_ask import WRITER_OUT, FakeModel, Harness, make_research
from core.agent.tools.enrich_tools import _comment_record
from core.api import agent_app, comment_links, privacy
from core.api.tests.test_privacy_projection import PrivStore, handle_row

COMMENT_WORDS = "agreed the log drum is everything"
OWN_URL = "https://www.tiktok.com/@some_other_account/video/99?comment=1"  # matches no post and names nobody hidden


class CommentModel(FakeModel):
    def __init__(self, comment_id):
        super().__init__()
        out = copy.deepcopy(WRITER_OUT)
        out["claims"].append({"id": "c5", "text": "A commenter agreed the log drum is everything.", "label": "single_source",
                              "kind": "observation", "evidence_ids": [comment_id],
                              "quotes": [{"evidence_id": comment_id, "text": COMMENT_WORDS}], "numbers": []})
        self.writer_out = out


COMMENT_ID = "tiktok_comment_4242"


def research_with_a_comment(ctx, prompt, options, emit, should_stop):
    out = make_research()(ctx, prompt, options, emit, should_stop)
    item = {"comment_id": "4242", "text": COMMENT_WORDS, "author": "a_commenter", "created_at": "2026-09-27T20:00:00+02:00",
            "url": OWN_URL, "likes": 4}
    record = _comment_record(item, ctx.evidence["tt_1"], "tiktok", "tiktok/post/comments")
    ctx.evidence[record["id"]] = record
    return out


def run_it():
    h = Harness(check=None, research=research_with_a_comment, model=CommentModel(COMMENT_ID))
    return h.run()


def stored(result):
    ask = agent_app.Ask({"ask_id": "a_20260928_test", "question": "What is behind amapiano?", "market": "ZA"})
    agent_app.execute(ask, lambda request, emit, stop: result)
    return ask.record


def test_the_run_returns_the_link_and_keeps_the_answer_in_the_release_a_shape():
    result = run_it()
    answer = result["answer"]
    assert "c5" in [c["id"] for c in answer["claims"]]
    assert result["evidence_links"] == {"v": 1, "parents": {COMMENT_ID: "tt_1"}}
    assert validate_answer(answer) == []
    assert all("parent_id" not in e for e in answer["evidence"])
    assert {"tt_1", COMMENT_ID} <= {e["id"] for e in answer["evidence"]}


def test_the_stored_record_holds_the_link_beside_the_answer():
    record = stored(run_it())
    assert record["evidence_links"] == {"v": 1, "parents": {COMMENT_ID: "tt_1"}}
    assert json.loads(agent_app.SINK[-1]["record"])["evidence_links"] == record["evidence_links"]
    assert comment_links.read(record) == {COMMENT_ID: "tt_1"}


def post_handle(record):
    return next(e["handle"] for e in record["answer"]["evidence"] if e["id"] == "tt_1")


def hide_handle(handle):
    store = PrivStore(hide=set())
    store.suppression_rows = [handle_row(handle=handle.lstrip("@"))]
    return store


def test_the_readers_drop_the_comment_when_its_post_creator_is_hidden():
    record = stored(run_it())
    out = privacy.project_record(record, hide_handle(post_handle(record)))
    shown = {e["id"] for e in out["answer"]["evidence"]}
    assert "tt_1" not in shown and COMMENT_ID not in shown
    assert "c5" not in [c["id"] for c in out["answer"]["claims"]]
    assert COMMENT_WORDS not in json.dumps(out) and "evidence_links" not in out


def test_the_readers_keep_the_comment_when_someone_else_is_hidden_because_the_link_says_whose_it_is():
    record = stored(run_it())
    out = privacy.project_record(record, hide_handle("unrelated_account"))
    assert COMMENT_ID in {e["id"] for e in out["answer"]["evidence"]}
    assert "c5" in [c["id"] for c in out["answer"]["claims"]]
    assert out["evidence_links"] == {"v": 1, "parents": {COMMENT_ID: "tt_1"}}


def test_without_the_link_the_same_comment_is_withheld_because_its_address_matches_no_post():
    record = stored(run_it())
    del record["evidence_links"]
    out = privacy.project_record(record, hide_handle("unrelated_account"))
    assert COMMENT_ID not in {e["id"] for e in out["answer"]["evidence"]}
