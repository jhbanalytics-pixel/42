"""The staging smoke test judges an Ask record with a blank summary. Ask blanks a summary in more than one way, and
core/api/tests/test_smoke.py varies only the gap that one of the ways writes. This matrix builds the record from each
real producer and pins what smoke says about it, so a change to a producer or to the smoke rule moves a named row.

The rows for the K6, K2 and K8 cuts are pinned at the verdict smoke gives today, which is a refusal: smoke accepts a
blank summary only beside the exact checked summary gap, and the code checks write a different gap. Whether that gap
shape should also be accepted is a ruling for Albert (the 8 October ZA smoke stopped here), so these rows say what
happens now and the rule is not loosened to any gap. If the ruling is to accept it, these three rows are the ones to
change, deliberately.
"""
import copy
import json
from pathlib import Path

import pytest

from core.agent import checks, writer
from core.agent.tests.test_checks import FakeWarehouse, ROWS, make_ctx, make_draft, run
from core.api import smoke

FIXTURE_ASK = Path(smoke.__file__).resolve().parent / "fixtures" / "ask_complete.json"


def record_with(answer):
    record = json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))
    record["answer"] = answer
    return record


def checked(edit):
    """check_answer's own answer for a draft the edit has changed."""
    ctx = make_ctx()
    draft = make_draft(ctx)
    edit(draft, ctx)
    answer, _ = run(draft, ctx, FakeWarehouse(ROWS))
    return answer


def breach_k6(draft, ctx):
    draft["short_answer"] = "Gen Z loves the Sunday plate this week."


def breach_k2(draft, ctx):
    draft["short_answer"] = "Sunday plate ratings rose 340% this week across 9,000 posts."


def breach_k8(draft, ctx):
    draft["short_answer"] = 'One creator called it "the plate of the century" on Sunday.'


def code_gap_for(answer, rule):
    return [g for g in answer["gaps"] if g["what"].startswith("Short answer removed") and f"({rule})" in g["what"]]


@pytest.mark.parametrize("edit, rule", [(breach_k6, "K6"), (breach_k2, "K2"), (breach_k8, "K8")])
def test_a_summary_the_code_checks_cut_is_refused_by_smoke_today(edit, rule):
    answer = checked(edit)
    assert answer["short_answer"] == "" and answer["status"] == "partial"
    assert len(code_gap_for(answer, rule)) == 1, answer["gaps"]
    assert writer.HEADLINE_GAP not in answer["gaps"]
    ok, why = smoke.check_ask_record(record_with(answer))
    assert ok is False and "summary" in why, why


def test_the_gap_the_code_checks_write_is_the_one_check_code_gap_builds():
    answer = checked(breach_k6)
    [gap] = code_gap_for(answer, "K6")
    assert gap == checks._code_gap("Short answer removed", "K6", "the short answer text")
    assert gap != writer.HEADLINE_GAP


def test_a_summary_the_support_check_blanked_is_accepted_beside_the_checked_summary_gap():
    answer = checked(lambda draft, ctx: None)
    answer.update(short_answer="", status="partial", gaps=[dict(writer.HEADLINE_GAP)])
    assert smoke.check_ask_record(record_with(answer))[0] is True


def test_a_narrowed_claim_that_blanked_the_summary_is_accepted_because_both_gaps_are_written():
    """ask.py appends HEADLINE_GAP and then NARROWED_HEADLINE_GAP, in that order, for a narrowed claim."""
    answer = checked(lambda draft, ctx: None)
    answer.update(short_answer="", status="partial", gaps=[dict(writer.HEADLINE_GAP), dict(writer.NARROWED_HEADLINE_GAP)])
    assert smoke.check_ask_record(record_with(answer))[0] is True


def test_the_narrowed_gap_alone_is_not_the_checked_summary_gap():
    answer = checked(lambda draft, ctx: None)
    answer.update(short_answer="", status="partial", gaps=[dict(writer.NARROWED_HEADLINE_GAP)])
    ok, why = smoke.check_ask_record(record_with(answer))
    assert ok is False and "summary" in why


def test_an_insufficient_evidence_answer_carries_its_own_summary_and_is_accepted():
    def one_claim(draft, ctx):
        draft["claims"] = draft["claims"][:1]
        draft["so_what"], draft["watch_next"] = [], []

    answer = checked(one_claim)
    assert answer["status"] == "insufficient_evidence" and answer["short_answer"] == checks.INSUFFICIENT
    assert smoke.check_ask_record(record_with(answer))[0] is True


def test_a_complete_answer_with_a_blank_summary_and_no_gap_is_refused():
    answer = checked(lambda draft, ctx: None)
    answer["short_answer"] = ""
    assert answer["status"] == "complete" and answer["gaps"] == []
    ok, why = smoke.check_ask_record(record_with(answer))
    assert ok is False and "summary" in why


def test_every_blank_summary_producer_in_the_matrix_has_a_row():
    """The producers of a blank short_answer, named here so a new one is a red test and not a silent smoke FAIL:
    writer.HEADLINE_GAP (and with it NARROWED_HEADLINE_GAP), the code-check cut on K6, K2 and K8, and
    insufficient_evidence, which is not blank. A new writer gap in CODE_GAPS that blanks the summary needs a row."""
    summary_gaps = [gap for gap in writer.CODE_GAPS if "summary" in gap["what"].lower()]
    assert {gap["what"] for gap in summary_gaps} == {writer.HEADLINE_GAP["what"], writer.NARROWED_HEADLINE_GAP["what"]}
    assert set(checks.RULE_GAPS) >= {"K6", "K2", "K8"}
    assert copy.deepcopy(writer.HEADLINE_GAP) == writer.HEADLINE_GAP


@pytest.mark.parametrize("gaps", [None, "not a list", {"what": "x"}])
def test_a_blank_summary_whose_gaps_are_not_a_list_is_a_clean_refusal(gaps):
    answer = checked(lambda draft, ctx: None)
    answer.update(short_answer="", status="partial", gaps=gaps)
    ok, why = smoke.check_ask_record(record_with(answer))
    assert ok is False and "summary" in why


def test_the_checked_summary_gap_beside_a_complete_status_is_refused():
    """ask.py lowers a complete answer that carries HEADLINE_GAP to partial, so complete with the gap is a record no
    producer writes."""
    answer = checked(lambda draft, ctx: None)
    answer.update(short_answer="", status="complete", gaps=[dict(writer.HEADLINE_GAP)])
    ok, why = smoke.check_ask_record(record_with(answer))
    assert ok is False and "summary" in why
