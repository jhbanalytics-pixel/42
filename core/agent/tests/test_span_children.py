"""Release D ruling: a transcript span is a child of its post, as a comment is (parent_id travels).

In Release C only a comment id was walked to its post. A span carries parent_id since Ask power, so the same four
places now follow it: check_answer puts the post of a cited span beside it, comment_links records the pair, and the two
prunes that rebuild an answer's evidence (writer.apply_support and ask._critic_cuts) keep the post when only the span
cites it. Nothing here changes a check: the claim passes the same K and G checks, and the post is added after them."""
import copy

from core.agent import checks, critic
from core.agent.answer import validate_answer
from core.agent.checks import check_answer
from core.agent.tests.test_ask import WRITER_OUT, FakeModel, Harness, make_research
from core.agent.tests.test_ask_power_transcripts import WINDOW, _cite, checks_with_transcript, claim
from core.agent.tests.test_ask_t2 import CriticModel, Lanes
from core.agent.tools.enrich_tools import _keep_spans

SPAN_WORDS = "the boerewors comes first"
SPOKEN = "I can't be the only one who thinks the boerewors comes first"


def test_an_answer_that_cites_only_a_span_carries_the_post_the_span_sits_under():
    ctx, ids, wh = checks_with_transcript()
    answer, _ = check_answer(_cite(ctx, ids[:1], ids[0], SPAN_WORDS), ctx, wh, window=WINDOW, market="ZA")
    assert claim(answer, "c1") is not None
    assert [e["id"] for e in answer["evidence"]] == ["tt_1", ids[0]]  # the post is read before its span


def test_a_post_that_is_cited_beside_its_span_is_listed_once():
    ctx, ids, wh = checks_with_transcript()
    answer, _ = check_answer(_cite(ctx, ["tt_1", ids[0]], ids[0], SPAN_WORDS), ctx, wh, window=WINDOW, market="ZA")
    listed = [e["id"] for e in answer["evidence"]]
    assert listed.count("tt_1") == 1 and listed.count(ids[0]) == 1 and listed.index("tt_1") < listed.index(ids[0])


def test_the_link_from_a_span_to_its_post_is_kept_beside_the_answer():
    ctx, ids, wh = checks_with_transcript()
    answer, _ = check_answer(_cite(ctx, ids[:1], ids[0], SPAN_WORDS), ctx, wh, window=WINDOW, market="ZA")
    assert checks.comment_links(answer, ctx.evidence) == {"v": 1, "parents": {ids[0]: "tt_1"}}


def test_the_links_of_a_comment_and_a_span_are_kept_together():
    stored = {"tt_1": {"id": "tt_1"}, "tiktok_comment_9": {"id": "tiktok_comment_9", "parent_id": "tt_1"},
              "tt_1_span_1": {"id": "tt_1_span_1", "parent_id": "tt_1"}}
    answer = {"evidence": [{"id": "tt_1"}, {"id": "tiktok_comment_9"}, {"id": "tt_1_span_1"}]}
    assert checks.comment_links(answer, stored) == {"v": 1, "parents": {"tiktok_comment_9": "tt_1", "tt_1_span_1": "tt_1"}}


def test_a_post_that_is_not_a_comment_or_a_span_is_never_walked_to_a_parent():
    stored = {"tt_1": {"id": "tt_1"}, "tt_2": {"id": "tt_2", "parent_id": "tt_1"}}
    assert checks.comment_links({"evidence": [{"id": "tt_1"}, {"id": "tt_2"}]}, stored) is None
    assert checks.cited_with_parents([{"evidence_ids": ["tt_2"]}], stored) == {"tt_2"}


def test_a_span_whose_post_is_not_in_the_run_adds_nothing():
    ctx, ids, wh = checks_with_transcript()
    draft = _cite(ctx, ids[:1], ids[0], SPAN_WORDS)
    del ctx.evidence["tt_1"]
    answer, _ = check_answer(draft, ctx, wh, window=WINDOW, market="ZA")
    assert [e["id"] for e in answer["evidence"]] == ids[:1]
    assert checks.comment_links(answer, ctx.evidence) is None


def test_the_cited_set_of_a_prune_holds_the_post_of_a_cited_span():
    ctx, ids, _ = checks_with_transcript()
    claims = [{"evidence_ids": ids[:1]}]
    assert checks.cited_with_parents(claims, ctx.evidence) == {"tt_1", ids[0]}


# The two prunes, through a real run: no other claim cites the post.

SPAN_ID = "tt_1_span_1"


def writer_out_citing_only_the_span():
    out = copy.deepcopy(WRITER_OUT)
    c1 = out["claims"][0]
    c1["evidence_ids"] = [e for e in c1["evidence_ids"] if e != "tt_1"]
    c1["quotes"] = [q for q in c1["quotes"] if q["evidence_id"] != "tt_1"]
    out["claims"].append({"id": "c5", "text": "A creator said the boerewors comes first.", "label": "single_source",
                          "kind": "observation", "evidence_ids": [SPAN_ID],
                          "quotes": [{"evidence_id": SPAN_ID, "text": SPAN_WORDS}], "numbers": []})
    return out


def add_span(ctx):
    _keep_spans(ctx, "tt_1", [{"start_s": 0.0, "end_s": 4.0, "text": SPOKEN}], "tiktok/post/transcript")


def research_with_a_span(ctx, prompt, options, emit, should_stop):
    out = make_research()(ctx, prompt, options, emit, should_stop)
    add_span(ctx)
    return out


class OnlyTheSpan(FakeModel):
    writer_out = writer_out_citing_only_the_span()


def assert_kept(result):
    answer = result["answer"]
    assert "c5" in [c["id"] for c in answer["claims"]]
    assert not any("tt_1" in c["evidence_ids"] for c in answer["claims"])  # nothing but the span's link names it
    ids = {e["id"] for e in answer["evidence"]}
    assert {"tt_1", SPAN_ID} <= ids
    assert result["evidence_links"] == {"v": 1, "parents": {SPAN_ID: "tt_1"}}
    assert validate_answer(answer) == []


def test_a_t1_run_where_only_the_span_cites_its_post_stores_the_post_and_the_link():
    assert_kept(Harness(check=None, research=research_with_a_span, model=OnlyTheSpan()).run())


class TwoLaneResearch(Lanes):
    def __call__(self, ctx, prompt, options, emit, should_stop):
        out = super().__call__(ctx, prompt, options, emit, should_stop)
        if "tt_1" in ctx.evidence and SPAN_ID not in ctx.evidence:
            add_span(ctx)
        return out


class SpanCriticModel(CriticModel):
    writer_out = writer_out_citing_only_the_span()


def test_a_t2_run_whose_critic_cuts_another_claim_still_stores_the_post_and_the_link():
    result = Harness(check=None, research=TwoLaneResearch(), model=SpanCriticModel({"c3": {"verdict": "cut"}})).run(tier="T2")
    assert critic.ROW_REASON["cut"]
    assert "c3" not in [c["id"] for c in result["answer"]["claims"]]
    assert_kept(result)
