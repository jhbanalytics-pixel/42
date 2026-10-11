"""Finding the story a short answer names or counts after every claim of it was cut.

The ranked rule asks the one line lead to name the stories. K10 blanks a short answer that restates a cut claim, but it
needs eight words in a row shared with the claim, which a lead made of story names never has. cut_story_in_lead finds a
lead that names a story whose claims were all cut, or opens with a count of stories larger than the stories that
survive. What is done about it is checks.replace_cut_story_lead's, tested in test_lead_cut_no_paid_call.py: the lead is
rebuilt in code, never blanked, so no paid rewrite follows."""
from pathlib import Path

import pytest

from core.agent import checks, writer
from core.eval import ask_replay

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ranked_briefing"
LEAD = "Three stories led South Africa this week: the Soweto unrest, the Pirates vs Sundowns cup final and the Forbes and Motsoane murder trial."


def text(name, rest="a line."):
    return f"{name}: {rest}"


KEPT = [text("Soweto unrest"), text("Cup final")]
TRIAL = [text("Forbes and Motsoane murder trial")]


def test_a_story_named_in_the_lead_whose_every_claim_was_cut_is_found():
    assert checks.cut_story_in_lead(LEAD, TRIAL, KEPT) == "Forbes and Motsoane murder trial"


def test_a_story_with_a_surviving_claim_is_not_cut():
    kept = [*KEPT, text("Forbes and Motsoane murder trial", "a second line.")]
    assert checks.cut_story_in_lead(LEAD, TRIAL, kept) is None


def test_the_name_is_matched_whatever_its_case_and_only_as_whole_words():
    assert checks.cut_story_in_lead("The SOWETO UNREST led.", [text("Soweto unrest")], [text("Cup final"), text("Trial")]) == "Soweto unrest"
    assert checks.cut_story_in_lead("The unrests led.", [text("unrest")], [text("Cup final"), text("Trial")]) is None


def test_a_curly_apostrophe_in_the_lead_still_names_the_story():
    assert checks.cut_story_in_lead("The Nando’s trial led.", [text("Nando's trial")], KEPT) == "Nando's trial"
    assert checks.cut_story_in_lead("The Nando's trial led.", [text("Nando’s trial")], KEPT) == "Nando’s trial"


def test_a_story_kept_under_one_apostrophe_and_cut_under_the_other_is_one_story():
    kept = [*KEPT, text("Nando's trial", "a second line.")]
    assert checks.cut_story_in_lead("The Nando’s trial led.", [text("Nando’s trial")], kept) is None


def test_a_story_cut_under_another_case_of_its_name_is_still_the_same_story_as_the_one_kept():
    kept = [*KEPT, text("FORBES AND MOTSOANE MURDER TRIAL")]
    assert checks.cut_story_in_lead(LEAD, TRIAL, kept) is None


# A count is a count of stories only when a story word follows it.

def test_a_lead_that_counts_more_stories_than_survive_is_found_when_a_story_was_cut():
    short = "Three stories led South Africa this week: the unrest, the cup final and the trial."
    assert checks.cut_story_in_lead(short, TRIAL, KEPT) is not None
    assert checks.cut_story_in_lead("Two stories led South Africa this week.", TRIAL, KEPT) is None
    assert checks.cut_story_in_lead(short, TRIAL, [*KEPT, text("Murder trial")]) is None


@pytest.mark.parametrize("lead", ["3 stories led.", "Two big stories led.", "Three conversations led South Africa this week.",
                                  "5 trends led the week.", "4 topics led.", "Three very big stories led."])
def test_a_numeral_followed_by_a_story_word_is_a_story_count(lead):
    assert checks.cut_story_in_lead(lead, TRIAL, KEPT[:1]) is not None  # one story survives, and the lead counts more


@pytest.mark.parametrize("lead", [
    "179 posts led the conversation this week.", "5 creators drove most of the amapiano talk this week.",
    "2,000 posts mentioned the fuel price this week, mostly on X.", "7 in 10 posts on the fuel price came from X.",
    "10 October was the busiest day for the fuel price talk.", "Two-thirds of the posts were on X.",
    "6 amapiano posts came from 5 creators on TikTok and X.", "This week three stories led."])
def test_a_numeral_that_counts_anything_else_is_no_story_count(lead):
    assert checks.cut_story_in_lead(lead, TRIAL, KEPT[:1]) is None


def test_a_count_with_no_story_cut_is_left_alone():
    assert checks.cut_story_in_lead("Three stories led South Africa this week.", ["No label here."], KEPT) is None


def test_a_lead_that_does_not_name_the_cut_story_is_left_alone():
    assert checks.cut_story_in_lead("Two stories led.", TRIAL, KEPT) is None


def test_an_answer_that_names_fewer_than_two_stories_is_not_a_ranking_and_is_left_alone():
    assert checks.cut_story_in_lead(LEAD, TRIAL, []) is None
    assert checks.cut_story_in_lead(LEAD, TRIAL, ["No label here."]) is None


def test_a_claim_with_no_story_name_is_never_a_cut_story():
    assert checks.cut_story_in_lead(LEAD, ["The trial drew crowds."], KEPT) is None
    assert checks.cut_story_in_lead("", TRIAL, KEPT) is None
    assert checks.cut_story_in_lead(None, TRIAL, KEPT) is None


@pytest.fixture(scope="module")
def runs():
    saved = ask_replay.FIXTURES
    ask_replay.FIXTURES = FIXTURES
    try:
        return {qid: ask_replay.run_question(qid) for qid in ("TOP-ZA", "TOP-ZA-SUPPORTCUT")}
    finally:
        ask_replay.FIXTURES = saved


def rows(result, claim_id, rule):
    return [r for r in result["claim_checks"] if r["claim_id"] == claim_id and r["rule"] == rule]


def test_with_no_cut_the_lead_stands(runs):
    answer = runs["TOP-ZA"]["answer"]
    assert answer["short_answer"].startswith("Three stories led") and "Forbes" in answer["short_answer"]
    assert not rows(runs["TOP-ZA"], "short_answer", "K10")


def test_a_support_check_that_cuts_a_whole_story_leaves_a_lead_written_from_the_survivors(runs):
    result = runs["TOP-ZA-SUPPORTCUT"]
    answer = result["answer"]
    assert [c["id"] for c in answer["claims"]] == ["c1", "c2", "c3", "c4", "c5", "c6", "c8"]
    assert answer["short_answer"] == "Two stories led South Africa this week: the Soweto unrest and the Pirates vs Sundowns cup final."
    assert "Forbes" not in answer["short_answer"]
    assert answer["status"] == "partial"


def test_a_rewrite_that_names_the_cut_story_again_is_refused():
    before = {"claims": [{"id": "c1", "text": text("Soweto unrest")}, {"id": "c2", "text": text("Cup final")},
                         {"id": "c7", "text": text("Forbes and Motsoane murder trial")}]}
    answer = {"claims": before["claims"][:2]}
    assert writer.names_cut_story("The Forbes and Motsoane murder trial led.", before, answer)
    assert not writer.names_cut_story("The Soweto unrest and the cup final led.", before, answer)


def test_the_short_answer_rewrite_is_refused_when_it_names_the_story_that_was_cut():
    from datetime import datetime, timezone
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
