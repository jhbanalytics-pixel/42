"""A short answer that names a story whose claims were all cut is blanked, so a ranked answer never leads with a story it
no longer holds or counts one it dropped.

The ranked rule asks the one line lead to name the stories. K10 blanks a short answer that restates a cut claim, but it
needs eight words in a row shared with the claim, which a lead made of story names never has. So a code check that cuts
a story's only claim left the lead naming it, with the page showing one block fewer. The test is one more way for K10 to
blank the lead: a story named in it, every claim of which was cut, while the answer names two or more stories. The
blank short answer is then the usual K10 state: a gap says so, the status is partial, and on the support path the one
rewrite writes a lead from the claims that survive."""
from pathlib import Path

import pytest

from core.agent import checks
from core.eval import ask_replay

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ranked_briefing"
LEAD = "Three stories led South Africa this week: the Soweto unrest, the Pirates vs Sundowns cup final and the Forbes and Motsoane murder trial."


def text(name, rest="a line."):
    return f"{name}: {rest}"


KEPT = [text("Soweto unrest"), text("Cup final")]


def test_a_story_named_in_the_lead_whose_every_claim_was_cut_is_found():
    assert checks.cut_story_in_lead(LEAD, [text("Forbes and Motsoane murder trial")], KEPT) == "Forbes and Motsoane murder trial"


def test_a_story_with_a_surviving_claim_is_not_cut():
    kept = [*KEPT, text("Forbes and Motsoane murder trial", "a second line.")]
    assert checks.cut_story_in_lead(LEAD, [text("Forbes and Motsoane murder trial")], kept) is None


def test_the_name_is_matched_whatever_its_case_and_only_as_whole_words():
    assert checks.cut_story_in_lead("The SOWETO UNREST led.", [text("Soweto unrest")], [text("Cup final"), text("Trial")]) == "Soweto unrest"
    assert checks.cut_story_in_lead("The unrests led.", [text("unrest")], [text("Cup final"), text("Trial")]) is None


def test_a_curly_apostrophe_in_the_lead_still_names_the_story():
    assert checks.cut_story_in_lead("The Nando’s trial led.", [text("Nando's trial")], KEPT) == "Nando's trial"
    assert checks.cut_story_in_lead("The Nando's trial led.", [text("Nando’s trial")], KEPT) == "Nando’s trial"


def test_a_story_cut_under_another_case_of_its_name_is_still_the_same_story_as_the_one_kept():
    kept = [*KEPT, text("FORBES AND MOTSOANE MURDER TRIAL")]
    assert checks.cut_story_in_lead(LEAD, [text("Forbes and Motsoane murder trial")], kept) is None


def test_a_lead_that_counts_more_stories_than_survive_is_found_when_a_story_was_cut():
    short = "Three stories led South Africa this week: the unrest, the cup final and the trial."
    assert checks.cut_story_in_lead(short, [text("Forbes and Motsoane murder trial")], KEPT) is not None
    assert checks.cut_story_in_lead("Two stories led South Africa this week.", [text("Forbes and Motsoane murder trial")], KEPT) is None
    kept = [*KEPT, text("Murder trial")]
    assert checks.cut_story_in_lead(short, [text("Forbes and Motsoane murder trial")], kept) is None
    assert checks.cut_story_in_lead("3 stories led.", [text("Forbes and Motsoane murder trial")], KEPT) is not None


def test_a_count_with_no_story_cut_is_left_alone():
    assert checks.cut_story_in_lead("Three stories led South Africa this week.", ["No label here."], KEPT) is None


def test_an_insufficient_evidence_short_answer_is_never_blanked_by_the_lead_cut():
    from datetime import datetime, timezone
    from core.agent.context import RunContext
    ctx = RunContext(run_id="r_20261011_0800", tier="T1", as_of=datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc), market="ZA")
    rows = []
    fields = {"short_answer": checks.INSUFFICIENT, "context": "", "so_what": [], "watch_next": [], "gaps": []}
    out, cut = checks._answer_fields(fields, [], ctx, lambda *a: rows.append(a), allow=checks._allowance(None, ctx.as_of),
                                     gap_indexes=range(0), lead_cut="Forbes and Motsoane murder trial")
    assert out["short_answer"] == checks.INSUFFICIENT and cut is False and not rows


def test_a_lead_that_does_not_name_the_cut_story_is_left_alone():
    assert checks.cut_story_in_lead("Two stories led.", [text("Forbes and Motsoane murder trial")], KEPT) is None


def test_an_answer_that_names_fewer_than_two_stories_is_not_a_ranking_and_is_left_alone():
    assert checks.cut_story_in_lead(LEAD, [text("Forbes and Motsoane murder trial")], []) is None
    assert checks.cut_story_in_lead(LEAD, [text("Forbes and Motsoane murder trial")], ["No label here."]) is None


def test_a_claim_with_no_story_name_is_never_a_cut_story():
    assert checks.cut_story_in_lead(LEAD, ["The trial drew crowds."], KEPT) is None
    assert checks.cut_story_in_lead("", [text("Forbes and Motsoane murder trial")], KEPT) is None
    assert checks.cut_story_in_lead(None, [text("Forbes and Motsoane murder trial")], KEPT) is None


@pytest.fixture(scope="module")
def runs():
    saved = ask_replay.FIXTURES
    ask_replay.FIXTURES = FIXTURES
    try:
        return {qid: ask_replay.run_question(qid) for qid in ("TOP-ZA", "TOP-ZA-K1CUT", "TOP-ZA-SUPPORTCUT")}
    finally:
        ask_replay.FIXTURES = saved


def rows(result, claim_id, rule):
    return [r for r in result["claim_checks"] if r["claim_id"] == claim_id and r["rule"] == rule]


def test_with_no_cut_the_lead_stands(runs):
    answer = runs["TOP-ZA"]["answer"]
    assert answer["short_answer"].startswith("Three stories led") and "Forbes" in answer["short_answer"]
    assert not rows(runs["TOP-ZA"], "short_answer", "K10")


def test_a_code_check_that_cuts_a_whole_story_blanks_the_lead_that_names_it(runs):
    result = runs["TOP-ZA-K1CUT"]
    answer = result["answer"]
    assert result["error"] is None and result["contract"] == []
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3", "c4", "c5", "c6", "c8"]
    assert [r["verdict"] for r in rows(result, "c7", "K1")] == ["cut"]
    assert answer["short_answer"] == "" and answer["status"] == "partial"
    lead_rows = rows(result, "short_answer", "K10")
    assert [r["verdict"] for r in lead_rows] == ["cut"] and lead_rows[0]["reason"] == checks.LEAD_STORY_REASON
    assert "Forbes" not in lead_rows[0]["reason"]  # the stored row names the rule, not the story
    assert any("Short answer removed" in gap["what"] for gap in answer["gaps"])
    assert "Forbes" not in str(answer["gaps"])  # a gap names the rule and the field, never the story


def test_a_support_check_that_cuts_a_whole_story_leaves_a_lead_written_from_the_survivors(runs):
    result = runs["TOP-ZA-SUPPORTCUT"]
    answer = result["answer"]
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3", "c4", "c5", "c6", "c8"]
    assert answer["short_answer"] == "Two stories led South Africa this week: the Soweto unrest and the Pirates vs Sundowns cup final."
    assert "Forbes" not in answer["short_answer"]
    assert answer["status"] == "partial"


def test_a_rewrite_that_names_the_cut_story_again_is_refused():
    from core.agent import writer
    before = {"claims": [{"id": "c1", "text": text("Soweto unrest")}, {"id": "c2", "text": text("Cup final")},
                         {"id": "c7", "text": text("Forbes and Motsoane murder trial")}]}
    answer = {"claims": before["claims"][:2]}
    assert writer.names_cut_story("The Forbes and Motsoane murder trial led.", before, answer)
    assert not writer.names_cut_story("The Soweto unrest and the cup final led.", before, answer)


def test_the_support_check_blanks_a_lead_that_names_a_story_it_cut():
    from core.agent import writer
    answer = {"status": "complete", "short_answer": LEAD}
    kept = [{"id": "c1", "text": KEPT[0]}, {"id": "c2", "text": KEPT[1]}]
    cut = {"c7": {"id": "c7", "text": text("Forbes and Motsoane murder trial")}}
    row = writer._k10(answer, kept, cut)
    assert answer["short_answer"] == "" and answer["status"] == "partial"
    assert row["verdict"] == "cut" and "Forbes and Motsoane murder trial" in row["reason"]


def test_the_support_check_keeps_a_lead_that_names_no_cut_story():
    from core.agent import writer
    answer = {"status": "complete", "short_answer": "Two stories led South Africa this week: the Soweto unrest and the cup final."}
    kept = [{"id": "c1", "text": KEPT[0]}, {"id": "c2", "text": KEPT[1]}]
    cut = {"c7": {"id": "c7", "text": text("Forbes and Motsoane murder trial")}}
    writer._k10(answer, kept, cut)
    assert answer["short_answer"].startswith("Two stories led")


def test_the_short_answer_rewrite_is_refused_when_it_names_the_story_that_was_cut():
    from datetime import datetime, timezone
    from core.agent import writer
    from core.agent.context import RunContext

    class Model:
        def complete_json(self, *, system, user, schema, model, max_tokens):
            return {"text": "The Soweto unrest and the Forbes and Motsoane murder trial led."}, {}

    ctx = RunContext(run_id="r_20261011_0800", tier="T1", as_of=datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc), market="ZA")
    before = {"claims": [{"id": "c1", "text": text("Soweto unrest")}, {"id": "c2", "text": text("Cup final")},
                         {"id": "c7", "text": text("Forbes and Motsoane murder trial")}]}
    answer = {"claims": before["claims"][:2]}
    got, _, dispatched, reason = writer.rewrite_headline(Model(), before, answer, ctx)
    assert got is None and dispatched and reason == writer.HEADLINE_REPEATS_REASON
