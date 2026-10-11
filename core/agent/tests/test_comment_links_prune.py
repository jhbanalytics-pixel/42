"""Privacy comments review 2, finding 1: a comment's post survives the two prunes that rebuild the answer's evidence
(writer.apply_support and ask._critic_cuts), so the stored record holds the post and the link even when only the comment
cites its post. Real run_ask with the offline fakes and the real code checks (check=None)."""
import copy

from core.agent import critic
from core.agent.answer import validate_answer
from core.agent.tests.test_ask import WRITER_OUT, FakeModel, Harness, make_research
from core.agent.tests.test_ask_t2 import CriticModel, Lanes
from core.agent.tools.enrich_tools import _comment_record

COMMENT_WORDS = "agreed the log drum is everything"
COMMENT_ID = "tiktok_comment_4242"
OWN_URL = "https://www.tiktok.com/@some_other_account/video/99?comment=1"


def writer_out_without_tt_1():
    """WRITER_OUT with a comment claim, and no claim but that one citing tt_1."""
    out = copy.deepcopy(WRITER_OUT)
    c1 = out["claims"][0]
    c1["evidence_ids"] = [e for e in c1["evidence_ids"] if e != "tt_1"]
    c1["quotes"] = [q for q in c1["quotes"] if q["evidence_id"] != "tt_1"]
    out["claims"].append({"id": "c5", "text": "A commenter agreed the log drum is everything.", "label": "single_source",
                          "kind": "observation", "evidence_ids": [COMMENT_ID],
                          "quotes": [{"evidence_id": COMMENT_ID, "text": COMMENT_WORDS}], "numbers": []})
    return out


def add_comment(ctx):
    item = {"comment_id": "4242", "text": COMMENT_WORDS, "author": "a_commenter", "created_at": "2026-09-27T20:00:00+02:00",
            "url": OWN_URL, "likes": 4}
    record = _comment_record(item, ctx.evidence["tt_1"], "tiktok", "tiktok/post/comments")
    ctx.evidence[record["id"]] = record


def research_with_a_comment(ctx, prompt, options, emit, should_stop):
    out = make_research()(ctx, prompt, options, emit, should_stop)
    add_comment(ctx)
    return out


class OnlyTheComment(FakeModel):
    writer_out = writer_out_without_tt_1()


def assert_kept(result):
    answer = result["answer"]
    assert "c5" in [c["id"] for c in answer["claims"]]
    assert not any("tt_1" in c["evidence_ids"] for c in answer["claims"])  # nothing but the comment's link names it
    ids = {e["id"] for e in answer["evidence"]}
    assert {"tt_1", COMMENT_ID} <= ids
    assert result["evidence_links"] == {"v": 1, "parents": {COMMENT_ID: "tt_1"}}
    assert validate_answer(answer) == []


def test_a_t1_run_where_only_the_comment_cites_its_post_stores_the_post_and_the_link():
    result = Harness(check=None, research=research_with_a_comment, model=OnlyTheComment()).run()
    assert_kept(result)


class TwoLaneResearch(Lanes):
    def __call__(self, ctx, prompt, options, emit, should_stop):
        out = super().__call__(ctx, prompt, options, emit, should_stop)
        if "tt_1" in ctx.evidence and COMMENT_ID not in ctx.evidence:
            add_comment(ctx)
        return out


class CommentCriticModel(CriticModel):
    writer_out = writer_out_without_tt_1()


def test_a_t2_run_whose_critic_cuts_another_claim_still_stores_the_post_and_the_link():
    model = CommentCriticModel({"c3": {"verdict": "cut"}})
    result = Harness(check=None, research=TwoLaneResearch(), model=model).run(tier="T2")
    assert critic.ROW_REASON["cut"]  # the critic ran and cut c3
    assert "c3" not in [c["id"] for c in result["answer"]["claims"]]
    assert_kept(result)
