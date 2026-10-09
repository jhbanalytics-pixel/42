"""The staging smoke test judges an Ask record with a blank summary. Ask blanks a summary in several ways, and each leaves
its own state in answer_meta (the C1 producer, core/agent/answer_state.py) and its own gap in the answer. This matrix
builds each record from the real producers, a real check_answer pass observed by the real SummaryLedger, built and
read back by the real answer_state, and pins the verdict of the release smoke (core/api/smoke.py) for each.

W8-DEC-07 is YES: a K6, K2 or K8 cut on the summary is a verified removal and the smoke accepts it when the record
carries the exact gap the code check writes. The rows below pin that, and pin the refusals beside it: a blank summary
with no verified removal, a removal whose gap is missing or belongs to another cause, an execution that did not
complete, a state that is not verified.

The module needs the producer (wave8/ask) and the release smoke (wave8/release). Until both are in the tree it skips
and the reason names the missing symbol, so the day they land it runs and must pass."""
import copy
import json
from pathlib import Path

import pytest

from core.api import smoke

answer_state = pytest.importorskip("core.agent.answer_state", reason="core.agent.answer_state (the C1 producer, wave8/ask) is not in this tree")
if not hasattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL"):
    pytest.skip("core.api.smoke.ACCEPT_ANY_VERIFIED_REMOVAL (wave8/release) is not in this tree", allow_module_level=True)

from core.agent import checks, plain, writer  # noqa: E402
from core.agent.tests.test_checks import FakeWarehouse, ROWS, make_ctx, make_draft, run  # noqa: E402

FIXTURE_ASK = Path(smoke.__file__).resolve().parent / "fixtures" / "ask_complete.json"
SHORT_ANSWER_GAP_TEXT = "the short answer text"


def base_record():
    return json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))


def seal(answer, *, summary, removals=(), rewrite="not_attempted", execution="completed", stop_reason=None, status="complete",
         check="verified"):
    """The record f42-api returns: the stored state built by the producer, read back through its own wire view."""
    record = {**base_record(), "ask_id": "a_matrix", "status": status, "answer": answer}
    record["run"] = {**(record.get("run") or {}), "run_id": "r_matrix"}
    record["answer_meta"] = answer_state.build(
        ask_id="a_matrix", check_run_id="r_matrix", execution_state=execution, stop_reason=stop_reason, summary_state=summary,
        removals=list(removals), rewrite=rewrite, answer=answer)
    record["answer_meta"] = answer_state.meta_view(record)
    assert record["answer_meta"]["check"] == check, record["answer_meta"]
    return record


def through_the_gate(edit):
    """A real first check pass on a draft the edit changed, observed by the real ledger: (answer as stored, ledger)."""
    ctx = make_ctx()
    draft = make_draft(ctx)
    edit(draft, ctx)
    before = answer_state.snapshot(draft)
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    ledger = answer_state.SummaryLedger()
    ledger.observe("first_check", before, answer, verdicts)
    return plain.answer(answer), ledger


def producer_record(edit):
    answer, ledger = through_the_gate(edit)
    summary, removals, rewrite = ledger.finalize(answer, "completed")
    return seal(answer, summary=summary, removals=removals, rewrite=rewrite, status="complete"), answer, removals


def breach_k6(draft, ctx):
    draft["short_answer"] = "Gen Z loves the Sunday plate this week."


def breach_k2(draft, ctx):
    draft["short_answer"] = "Sunday plate ratings rose 340% this week across 9,000 posts."


def breach_k8(draft, ctx):
    draft["short_answer"] = 'One creator called it "the plate of the century" on Sunday.'


def code_gap(rule):
    return plain.gap(checks._code_gap("Short answer removed", rule, SHORT_ANSWER_GAP_TEXT))


@pytest.mark.parametrize("edit, rule", [(breach_k6, "K6"), (breach_k2, "K2"), (breach_k8, "K8")])
def test_a_summary_the_code_checks_cut_is_a_verified_removal_and_smoke_accepts_it(edit, rule):
    record, answer, removals = producer_record(edit)
    assert answer["short_answer"] == "" and answer["status"] == "partial"
    assert removals == [("first_check", rule)]
    assert code_gap(rule) in answer["gaps"] and writer.HEADLINE_GAP not in answer["gaps"]
    assert record["answer_meta"]["summary"]["state"] == "removed"
    ok, why = smoke.check_ask_record({**record, "status": "complete"})
    assert ok is True, why


@pytest.mark.parametrize("edit, rule", [(breach_k6, "K6"), (breach_k2, "K2"), (breach_k8, "K8")])
def test_the_same_cut_is_refused_when_its_gap_is_missing_altered_or_for_another_cause(edit, rule):
    record, answer, _ = producer_record(edit)
    other = "K8" if rule != "K8" else "K2"
    for gaps in ([], [dict(writer.HEADLINE_GAP)], [code_gap(other)],
                 [{**code_gap(rule), "why": "another reason"}], [{**code_gap(rule), "what": code_gap(rule)["what"] + " and more"}]):
        broken = copy.deepcopy(record)
        broken["answer"]["gaps"] = gaps
        ok, why = smoke.check_ask_record(broken)
        assert ok is False and smoke.BLANK_PROBLEM in why, (gaps, why)


def test_without_the_acceptance_switch_the_code_check_cuts_are_refused(monkeypatch):
    """ACCEPT_ANY_VERIFIED_REMOVAL is the one switch W8-DEC-07 turned on. Off, a cut needs the support-check gap."""
    record, _, _ = producer_record(breach_k6)
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", False)
    assert smoke.check_ask_record(record)[0] is False


def writer_answer(*gaps):
    answer, _ = through_the_gate(lambda draft, ctx: None)
    answer = copy.deepcopy(answer)
    answer.update(short_answer="", status="partial", gaps=[dict(g) for g in gaps])
    return answer


@pytest.mark.parametrize("stage, cause, gaps", [
    ("support_check", "claim_cut", [writer.HEADLINE_GAP]),
    ("critic", "claim_cut", [writer.HEADLINE_GAP]),
    ("support_check", "claim_narrowed", [writer.HEADLINE_GAP, writer.NARROWED_HEADLINE_GAP])])
def test_a_summary_the_writer_side_checks_removed_is_accepted_with_the_gaps_the_producer_writes(stage, cause, gaps):
    answer = writer_answer(*gaps)
    record = seal(answer, summary="removed", removals=[(stage, cause)])
    ok, why = smoke.check_ask_record(record)
    assert ok is True, why


def test_a_narrowed_claim_without_the_narrowed_gap_is_refused():
    answer = writer_answer(writer.HEADLINE_GAP)
    ok, why = smoke.check_ask_record(seal(answer, summary="removed", removals=[("support_check", "claim_narrowed")]))
    assert ok is False and smoke.BLANK_PROBLEM in why


def test_an_insufficient_evidence_answer_carries_the_fixed_text_and_is_accepted():
    def one_claim(draft, ctx):
        draft["claims"] = draft["claims"][:1]
        draft["so_what"], draft["watch_next"] = [], []

    answer, ledger = through_the_gate(one_claim)
    assert answer["status"] == "insufficient_evidence" and answer["short_answer"] == checks.INSUFFICIENT
    summary, removals, rewrite = ledger.finalize(answer, "completed")
    assert summary == "fixed_text"
    ok, why = smoke.check_ask_record(seal(answer, summary=summary, removals=removals, rewrite=rewrite, status="complete"))
    assert ok is True, why


def test_a_summary_that_is_shown_is_accepted():
    answer, ledger = through_the_gate(lambda draft, ctx: None)
    assert answer["short_answer"].strip() and answer["gaps"] == []
    summary, removals, rewrite = ledger.finalize(answer, "completed")
    assert smoke.check_ask_record(seal(answer, summary=summary, removals=removals, rewrite=rewrite))[0] is True


def test_a_blank_summary_with_no_removal_on_record_is_refused():
    answer = writer_answer(writer.HEADLINE_GAP)
    record = seal(answer, summary="blank_unexplained")
    ok, why = smoke.check_ask_record(record)
    assert ok is False and smoke.BLANK_PROBLEM in why


def test_an_unattributed_removal_is_refused_because_the_producer_writes_no_gap_for_it():
    answer = writer_answer(writer.HEADLINE_GAP)
    ok, why = smoke.check_ask_record(seal(answer, summary="removed", removals=[("support_check", "unattributed")]))
    assert ok is False and smoke.BLANK_PROBLEM in why


@pytest.mark.parametrize("execution, stop_reason", [("stopped_on_request", None), ("stopped_on_budget", "budget_full")])
def test_a_removed_summary_in_a_run_that_did_not_complete_is_not_a_verified_state(execution, stop_reason):
    """The producer's own pairing rule marks the combination unverified, and the smoke refuses an unverified state."""
    answer = writer_answer(writer.HEADLINE_GAP)
    record = seal(answer, summary="removed", removals=[("support_check", "claim_cut")], execution=execution,
                  stop_reason=stop_reason, status="stopped", check="unverified")
    assert record["answer_meta"]["problem"] == "pairing"
    ok, why = smoke.check_ask_record(record)
    assert ok is False and "no verified answer state" in why, why


def test_a_blank_summary_stated_as_shown_is_refused():
    answer = writer_answer(writer.HEADLINE_GAP)
    record = {**base_record(), "ask_id": "a_matrix", "status": "complete", "answer": answer}
    record["answer_meta"] = answer_state.build(
        ask_id="a_matrix", check_run_id=None, execution_state="completed", stop_reason=None, summary_state="shown",
        removals=[], rewrite="not_attempted", answer=answer)
    record["answer_meta"] = answer_state.meta_view(record)
    ok, why = smoke.check_ask_record(record)
    assert ok is False, why


@pytest.mark.parametrize("meta", [None, {"check": "legacy_unknown"}, {"check": "unverified", "problem": "digest"}, "verified"])
def test_a_record_without_a_verified_state_is_refused_before_the_summary_is_read(meta):
    record, _, _ = producer_record(breach_k6)
    if meta is None:
        record.pop("answer_meta")
    else:
        record["answer_meta"] = meta
    ok, why = smoke.check_ask_record(record)
    assert ok is False and "no verified answer state" in why, why


def test_a_state_that_disagrees_with_its_answer_is_refused():
    """The wire value carries no digest, so check_wire recomputes the bound facts from the answer in the same record."""
    record, _, _ = producer_record(breach_k6)
    record["answer"]["short_answer"] = "A summary that was not removed after all."
    ok, why = smoke.check_ask_record(record)
    assert ok is False, why

