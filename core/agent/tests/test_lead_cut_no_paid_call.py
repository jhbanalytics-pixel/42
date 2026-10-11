"""A lead that names or counts a story whose claims were all cut is rebuilt in code, and the ask pays for no call it did
not pay for before the lead cut.

The short answer rewrite is one paid model call. It runs when the support check blanks a lead the code checks kept, and
that is the only time it ran before the lead cut, so the lead cut blanks nothing: it replaces the lead, from the names of
the stories that survive, in code. A replaced lead is a summary that is shown, so the typed summary state records no
removal and the release smoke has nothing unattributed to refuse (it passes on every replay below, the lead-cut ones
included). dispatches_062690d.json holds the rewrites each replay dispatched in the tree before the lead cut."""
import copy
import json
from pathlib import Path

import pytest

from core.agent import answer_state, checks, writer
from core.api import smoke
from core.eval import ask_replay

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BEFORE = json.loads((FIXTURES / "lead_cut" / "dispatches_062690d.json").read_text(encoding="utf-8"))
WHERE = {"TOP-ZA": "ranked_briefing", "TOP-ZA-K1CUT": "ranked_briefing", "TOP-ZA-SUPPORTCUT": "ranked_briefing",
         "K1CUT-COUNT": "lead_cut", "K1CUT-UNRANKED": "lead_cut"}
CODE_LEAD = "The stories that led: Soweto unrest and Pirates vs Sundowns cup final."


@pytest.fixture(scope="module")
def runs():
    saved, found, calls = ask_replay.FIXTURES, {}, {"n": 0}
    original = ask_replay.ReplayModel.complete_json

    def spy(self, *, system, user, schema, model, max_tokens):
        if schema is writer.HEADLINE_REWRITE_SCHEMA:
            calls["n"] += 1
        return original(self, system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    ask_replay.ReplayModel.complete_json = spy
    try:
        for qid, folder in WHERE.items():
            ask_replay.FIXTURES = FIXTURES / folder
            calls["n"] = 0
            found[qid] = ask_replay.run_question(qid)
            found[qid]["rewrites"] = calls["n"]
    finally:
        ask_replay.ReplayModel.complete_json = original
        ask_replay.FIXTURES = saved
    return found


def wire(result):
    stored = {"ask_id": result["answer_meta"]["ask_id"], "status": "complete", "answer": result["answer"], "run": result["run"],
              "answer_meta": result["answer_meta"]}
    return {**stored, "answer_meta": answer_state.meta_view(stored)}


@pytest.mark.parametrize("qid", sorted(WHERE))
def test_the_rewrite_is_dispatched_exactly_as_often_as_before_the_lead_cut(qid, runs):
    assert runs[qid]["error"] is None
    assert runs[qid]["rewrites"] == BEFORE[qid]["rewrites"]


def test_the_two_cases_that_paid_for_no_rewrite_before_pay_for_none_now(runs):
    assert BEFORE["TOP-ZA-K1CUT"]["rewrites"] == 0 and BEFORE["K1CUT-COUNT"]["rewrites"] == 0
    assert runs["TOP-ZA-K1CUT"]["rewrites"] == 0 and runs["K1CUT-COUNT"]["rewrites"] == 0


def test_a_support_cut_that_blanked_the_lead_before_still_gets_its_one_rewrite(runs):
    assert BEFORE["TOP-ZA-SUPPORTCUT"]["rewrites"] == 1 and runs["TOP-ZA-SUPPORTCUT"]["rewrites"] == 1
    assert runs["TOP-ZA-SUPPORTCUT"]["answer"]["short_answer"] == BEFORE["TOP-ZA-SUPPORTCUT"]["lead"]


def test_a_lead_that_names_a_story_the_code_checks_cut_is_rebuilt_from_the_stories_that_survive(runs):
    answer = runs["TOP-ZA-K1CUT"]["answer"]
    assert answer["short_answer"] == CODE_LEAD and answer["status"] == "partial"
    assert [c["id"] for c in answer["claims"]] == BEFORE["TOP-ZA-K1CUT"]["claims"]
    assert "Forbes" not in answer["short_answer"]


def test_a_lead_that_only_counts_the_stories_is_rebuilt_the_same_way(runs):
    assert runs["K1CUT-COUNT"]["answer"]["short_answer"] == CODE_LEAD


def test_the_lead_cut_does_not_apply_to_a_question_that_is_no_ranked_question(runs):
    assert runs["K1CUT-UNRANKED"]["answer"]["short_answer"] == BEFORE["K1CUT-UNRANKED"]["lead"]


def test_the_rebuilt_lead_leaves_a_row_and_a_gap_that_name_the_rule_and_no_story(runs):
    result = runs["TOP-ZA-K1CUT"]
    rows = [r for r in result["claim_checks"] if r["claim_id"] == "short_answer" and r["rule"] == "K10"]
    assert [(r["verdict"], r["reason"]) for r in rows] == [("downgrade", checks.LEAD_STORY_REASON)]
    gaps = [g["what"] for g in result["answer"]["gaps"] if "Short answer" in g["what"]]
    assert gaps == [checks.LEAD_STORY_GAP["what"].replace(" (K10)", "")]
    assert "Forbes" not in json.dumps(result["answer"]["gaps"]) and "Forbes" not in checks.LEAD_STORY_REASON


@pytest.mark.parametrize("qid", sorted(WHERE))
def test_the_summary_is_shown_with_no_removal_and_the_state_verifies(qid, runs):
    result = runs[qid]
    assert result["meta_problem"] is None
    summary = result["answer_meta"]["summary"]
    if qid == "TOP-ZA-SUPPORTCUT":  # the support check cut a claim and the one rewrite wrote the lead, as before
        assert summary["state"] == "shown_rewritten" and summary["removals"] == [{"stage": "support_check", "cause": "claim_cut"}]
    else:
        assert summary["state"] == "shown" and summary["removals"] == []


@pytest.mark.parametrize("qid", sorted(WHERE))
def test_the_release_smoke_passes_on_every_replay_the_lead_cut_ones_included(qid, runs):
    ok, why = smoke.check_ask_record(wire(runs[qid]))
    assert ok, why


def test_no_replay_stores_an_unattributed_removal():
    saved, every = ask_replay.FIXTURES, []
    try:
        for folder in (Path(ask_replay.__file__).parent / "ask_replay_fixtures", FIXTURES / "ranked_briefing",
                       FIXTURES / "lead_cut"):
            ask_replay.FIXTURES = folder
            every += [ask_replay.run_question(p.stem) for p in sorted(folder.glob("*.json"))
                      if p.stem != "dispatches_062690d"]
    finally:
        ask_replay.FIXTURES = saved
    causes = [r["cause"] for res in every for r in (res["answer_meta"] or {}).get("summary", {}).get("removals", [])]
    assert len(every) == 18 and "unattributed" not in causes


# The support step blanks a lead exactly when it did before: the lead cut adds no blank to it.

def test_the_support_step_keeps_a_lead_that_names_a_story_it_cut_when_the_lead_shares_no_words_with_the_claim():
    lead = "Three stories led South Africa this week: the unrest, the cup final and the trial."
    answer = {"status": "complete", "short_answer": lead}
    kept = [{"id": "c1", "text": "Soweto unrest: a line."}, {"id": "c2", "text": "Cup final: a line."}]
    cut = {"c7": {"id": "c7", "text": "Murder trial: followed through court updates."}}
    writer._k10(answer, kept, cut)
    assert answer["short_answer"] == lead and answer["status"] == "partial"


# The code that rebuilds the lead.

def story(name, rest="a line."):
    return {"id": name[:3], "text": f"{name}: {rest}"}


def answer_of(lead, kept):
    return {"status": "partial", "short_answer": lead, "claims": kept, "gaps": []}


GONE = [story("Murder trial")["text"]]
KEPT = [story("Soweto unrest"), story("Cup final"), {"id": "x", "text": "Soweto unrest: a second line."}]


def test_the_lead_is_rebuilt_from_the_surviving_names_once_each_in_the_order_written():
    out, row = checks.replace_cut_story_lead(answer_of("The murder trial led.", copy.deepcopy(KEPT)), GONE, ranked=True)
    assert out["short_answer"] == "The stories that led: Soweto unrest and Cup final."
    assert row["rule"] == "K10" and row["verdict"] == "downgrade" and row["reason"] == checks.LEAD_STORY_REASON
    assert checks.LEAD_STORY_GAP in out["gaps"]


def test_three_names_read_as_a_list_and_one_name_as_one_story():
    three = [*KEPT, story("Sunday braai")]
    got = checks.replace_cut_story_lead(answer_of("The murder trial led.", three), GONE, ranked=True)[0]["short_answer"]
    assert got == "The stories that led: Soweto unrest, Cup final and Sunday braai."
    one = [story("Soweto unrest")]
    got = checks.replace_cut_story_lead(answer_of("The murder trial led.", one), GONE, ranked=True)[0]["short_answer"]
    assert got == "The stories that led: Soweto unrest."


def test_nothing_changes_for_an_unranked_question_a_blank_lead_a_fixed_text_or_a_lead_naming_no_cut_story():
    cases = [(answer_of("The murder trial led.", copy.deepcopy(KEPT)), False),
             (answer_of("", KEPT), True),
             (answer_of(checks.INSUFFICIENT, KEPT), True),
             (answer_of("Two stories led.", KEPT), True),
             (answer_of("The murder trial led.", []), True),
             (answer_of("The murder trial led.", [{"id": "x", "text": "No label here."}]), True)]
    for answer, ranked in cases:
        out, row = checks.replace_cut_story_lead(answer, GONE, ranked=ranked)
        assert row is None and out == answer


def test_the_input_is_not_changed():
    answer = answer_of("The murder trial led.", copy.deepcopy(KEPT))
    before = copy.deepcopy(answer)
    checks.replace_cut_story_lead(answer, GONE, ranked=True)
    assert answer == before
