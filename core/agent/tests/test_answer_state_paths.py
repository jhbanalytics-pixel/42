"""C1 (3.4a): the producer. run_ask returns an `answer_meta` built from what the gate actually did, never from the
model's output or from gap text. Tests A-03b, A-05, A-06 to A-10 and A-14.

The expected state of each scenario is a literal typed here from the contract's catalogue (C1 section 7), not computed
by the module that produces it: the producer and the expectation do not read each other.
"""

import json
import logging
import re
from datetime import date

import pytest

from core.agent import answer_state, ask, checks, writer
from core.agent.tests.test_ask import WRITER_OUT, FakeModel, Harness, current_model_cap, make_research
from core.agent.tests.test_ask_headline import HeadlineModel, NarrowingModel, schemas
from core.agent.tests.test_ask_t2 import CriticModel, RedraftedGapModel, Lanes, t2
from core.agent.writer import HEADLINE_GAP, HEADLINE_REWRITE_SCHEMA, SUPPORT_SCHEMA, WRITER_SCHEMA

ASK_ID = "a_20260928_test"
RISING_K6 = "Amapiano posts came from creators on TikTok and X, and teenagers love it."
RISING_K2 = "Amapiano posts came from 9 creators on TikTok and X."


def removed(*pairs, rewrite="not_attempted"):
    return {"state": "removed", "removals": [{"stage": s, "cause": c} for s, c in pairs], "rewrite": rewrite}


def record_of(out, status="complete"):
    return {"ask_id": ASK_ID, "status": status, "answer": out["answer"], "run": out["run"],
            "answer_meta": out["answer_meta"]}


def check(out, execution, summary, status="complete", stop=None):
    meta = out["answer_meta"]
    assert meta["execution"] == {"state": execution, "stop_reason": stop}
    assert meta["summary"] == summary
    assert meta["ask_id"] == ASK_ID and meta["check_run_id"] == out["run"]["run_id"]
    assert answer_state.verify_stored(record_of(out, status)) is None
    wired = {**record_of(out, status), "answer_meta": answer_state.meta_view(record_of(out, status))}
    assert wired["answer_meta"]["check"] == "verified" and answer_state.check_wire(wired) is None
    return meta


def gap_set(out):
    return out["answer"]["gaps"]


# F01. A normal answer: the summary stands.
def test_a06_a_summary_that_survives_is_shown_with_no_removals():
    class KeepsEveryClaim(HeadlineModel):
        writer_out = {**WRITER_OUT, "claims": [WRITER_OUT["claims"][0], WRITER_OUT["claims"][2]],
                      "so_what": WRITER_OUT["so_what"][:1]}

    out = Harness(model=KeepsEveryClaim()).run()
    check(out, "completed", {"state": "shown", "removals": [], "rewrite": "not_attempted"})
    assert out["answer"]["short_answer"] == WRITER_OUT["short_answer"]
    assert HEADLINE_GAP not in gap_set(out)


# F02. A support cut blanked it and the one rewrite was kept: no removal gap for a shown summary.
def test_a06_a_rewrite_that_passes_is_shown_rewritten_with_the_support_removal_listed():
    out = Harness(model=HeadlineModel()).run()
    meta = check(out, "completed", {"state": "shown_rewritten",
                                    "removals": [{"stage": "support_check", "cause": "claim_cut"}], "rewrite": "kept"})
    assert meta["bound"] == {"answer_status": "partial", "summary_blank": False, "claims": 2}
    assert HEADLINE_GAP not in gap_set(out)


# F03. A narrowed claim kept, rewrite kept: the answer stays complete.
def test_a06_a_narrowed_claim_with_a_kept_rewrite_is_shown_rewritten_on_a_complete_answer():
    out = Harness(model=NarrowingModel()).run()
    meta = check(out, "completed", {"state": "shown_rewritten",
                                    "removals": [{"stage": "support_check", "cause": "claim_narrowed"}],
                                    "rewrite": "kept"})
    assert meta["bound"]["answer_status"] == "complete" and meta["bound"]["summary_blank"] is False


# F12a, F12b, F13. The rewrite did not happen or was held back: the summary stays removed.
@pytest.mark.parametrize("out_value, outcome", [
    ({"text": "  "}, "empty"),
    ({"verdict": "supported"}, "call_failed"),
    ({"text": "Amapiano posts came from creators on TikTok and X (c1)."}, "repeated_removed_claim"),
])
def test_a06_a_rewrite_the_code_holds_back_or_that_fails_leaves_removed_with_the_outcome(out_value, outcome):
    out = Harness(model=HeadlineModel(out_value)).run()
    check(out, "completed", removed(("support_check", "claim_cut"), rewrite=outcome))
    assert out["answer"]["short_answer"] == "" and HEADLINE_GAP in gap_set(out)


# F14, F14b, F15a. The rewrite passed and was then removed by a later check.
@pytest.mark.parametrize("text, cause", [(RISING_K6, "K6"), (RISING_K2, "K2")])
def test_a06_a_rewrite_removed_by_the_recheck_is_removed_after_check_with_the_rule(text, cause):
    out = Harness(model=HeadlineModel({"text": text})).run()
    check(out, "completed", removed(("support_check", "claim_cut"), ("recheck", cause), rewrite="removed_after_check"))


def test_a06_a_rewrite_the_field_check_flags_is_removed_after_check_by_the_field_check():
    out = Harness(model=HeadlineModel(forecast_headline=True)).run()
    check(out, "completed", removed(("support_check", "claim_cut"), ("field_check", "K9"),
                                    rewrite="removed_after_check"))


# F04. Fewer than two claims survive: the code writes the summary and the ledger resets.
def test_a06_fewer_than_two_survivors_is_fixed_text_with_no_removals():
    class CutsC3Too(HeadlineModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is SUPPORT_SCHEMA and "challenge is spreading" in user:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                return {"verdict": "unsupported", "reason": "not shown", "demographic_inference": False,
                        "tone_claim": False, "forecast_assertion": False, "country_people": []}, \
                    {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    out = Harness(model=CutsC3Too()).run()
    check(out, "completed", {"state": "fixed_text", "removals": [], "rewrite": "not_attempted"})
    assert out["answer"]["short_answer"] == checks.INSUFFICIENT


# F17. A T2 critic cut a claim the kept rewrite rested on.
def test_a06_a_critic_cut_after_a_kept_rewrite_is_removed_after_check_with_both_removals():
    survivor = {"id": "c5", "text": "Amapiano posts on X talked about braai gatherings and the speakers at every "
                                    "taxi rank.",
                "label": "single_source", "kind": "observation", "evidence_ids": ["x_1", "x_2"], "quotes": [],
                "numbers": []}

    class Model(CriticModel):
        writer_out = {**WRITER_OUT, "claims": [*WRITER_OUT["claims"], survivor]}

        def complete_json(self, *, system, user, schema, model, max_tokens):
            if schema is HEADLINE_REWRITE_SCHEMA:
                self.calls.append({"model": model, "schema": schema, "user": user, "max_tokens": max_tokens})
                return {"text": survivor["text"]}, {"input_tokens": 100, "output_tokens": 20, "usd": 0.0006}
            return super().complete_json(system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    out = t2(model=Model({"c5": {"verdict": "cut"}})).run(tier="T2")
    check(out, "completed", removed(("support_check", "claim_cut"), ("critic", "claim_cut"),
                                    rewrite="removed_after_check"))
    assert gap_set(out).count(HEADLINE_GAP) == 1


# F18. The model wrote nothing.
def test_a06_a_summary_the_model_never_wrote_is_blank_unexplained():
    class Silent(FakeModel):
        writer_out = {**WRITER_OUT, "short_answer": "", "claims": [WRITER_OUT["claims"][0], WRITER_OUT["claims"][2]],
                      "so_what": WRITER_OUT["so_what"][:1]}

    out = Harness(model=Silent()).run()
    check(out, "completed", {"state": "blank_unexplained", "removals": [], "rewrite": "not_attempted"})


# F11 and F10 shape: the first check removes the summary (K6 in the draft's own summary).
@pytest.mark.parametrize("summary, cause", [
    ("Amapiano is loved by teenagers across South Africa this week.", "K6"),
    ("Amapiano posts came from 9 creators across South Africa this week.", "K2"),
])
def test_a07_a_draft_summary_the_first_check_removes_is_removed_at_first_check_with_no_rewrite(summary, cause):
    class DraftHasIt(HeadlineModel):
        writer_out = {**WRITER_OUT, "short_answer": summary,
                      "claims": [WRITER_OUT["claims"][0], WRITER_OUT["claims"][2]],
                      "so_what": WRITER_OUT["so_what"][:1]}

    h = Harness(check=None, model=DraftHasIt())
    out = h.run()
    check(out, "completed", removed(("first_check", cause)))
    assert "headline" not in schemas(h)  # the support stage never saw a summary to blank, so no rewrite call
    assert HEADLINE_GAP not in gap_set(out)


# A-10. Execution states.
def test_a10_a_user_stop_is_stopped_on_request_with_the_fixed_text():
    flag = {"stop": False}
    out = Harness(research=make_research(stop_flag=flag), stop_flag=flag).run()
    check(out, "stopped_on_request", {"state": "fixed_text", "removals": [], "rewrite": "not_attempted"},
          status="stopped")
    assert out["answer"]["short_answer"] == ask.STOP_REQUEST_TEXT
    assert out["answer_meta"]["bound"] == {"answer_status": "insufficient_evidence", "summary_blank": False,
                                           "claims": 0}


def test_a10_a_spent_daily_budget_is_refused_with_the_fixed_text():
    out = Harness(research=make_research(seen={}), spent=lambda: current_model_cap() + 5.0).run()
    check(out, "refused_budget_spent", {"state": "fixed_text", "removals": [], "rewrite": "not_attempted"})
    assert out["answer"]["short_answer"] == ask.REFUSAL_TEXT


def test_a10_the_two_inline_texts_are_lifted_to_named_constants_without_changing_a_character():
    assert ask.STOP_REQUEST_TEXT == "The run was stopped before an answer was written, so there is nothing checked to show."
    assert ask.REFUSAL_TEXT == "This question was not researched because the model budget for today is spent."


@pytest.mark.parametrize("reason, stop_reason, text", [
    ("usage_unknown", "model_call_unverified", ask._STOP_FAILED[1]),
    ("model_price_invalid", "price_unreadable", ask._STOP_PRICE[1]),
    ("something_new", "budget_full", ask._STOP_BUDGET[1]),
    (None, "budget_full", ask._STOP_BUDGET[1]),
])
def test_a10_each_budget_stop_reason_maps_to_its_stop_reason_and_its_text(reason, stop_reason, text):
    state = ask.execution_of(finished="stopped", budget_stopped=True, reason=reason, refused=False)
    assert state == ("stopped_on_budget", stop_reason)
    assert ask.budget_stop_words(reason)[1] == text
    answer = ask._stopped_answer("2026-09-28", [], (date(2026, 9, 22), date(2026, 9, 28)), budget_stopped=True,
                                 reason=reason)
    assert answer["short_answer"] == text


def test_a10_execution_of_the_other_states():
    assert ask.execution_of(finished=None, budget_stopped=False, reason=None, refused=False) == ("completed", None)
    assert ask.execution_of(finished="stopped", budget_stopped=False, reason=None, refused=False) == (
        "stopped_on_request", None)
    assert ask.execution_of(finished=None, budget_stopped=False, reason=None, refused=True) == (
        "refused_budget_spent", None)


def test_a10_a_budget_stop_raised_mid_gate_is_stopped_on_budget_with_its_reason():
    def stops(draft, ctx, warehouse, window, markets):
        exc = ask._StopRequested()
        exc.model_budget_reason = "usage_unknown"
        raise exc

    out = Harness(check=stops).run()
    check(out, "stopped_on_budget", {"state": "fixed_text", "removals": [], "rewrite": "not_attempted"},
          stop="model_call_unverified")
    assert out["answer"]["short_answer"] == ask._STOP_FAILED[1]


def test_the_critic_stage_recheck_records_a_removal_the_recheck_makes(monkeypatch):
    # The recheck inside _critic_cuts runs after the critic's own blanking; here the critic cuts a claim the summary
    # does not rest on, and the recheck then removes the summary.
    answer = {"status": "partial", "as_of": "2026-09-28", "short_answer": "A summary.", "claims": [
        {"id": "c1", "text": "one", "evidence_ids": ["tt_1"]}, {"id": "c2", "text": "two", "evidence_ids": ["tt_2"]}],
        "evidence": [], "so_what": [], "watch_next": [], "gaps": [], "context": ""}

    def recheck(ans, ctx, window, code_gaps):
        return {**ans, "short_answer": ""}, [{"claim_id": "short_answer", "rule": "K6", "verdict": "cut"}]

    monkeypatch.setattr(ask.checks, "recheck_fields", recheck)
    monkeypatch.setattr(ask, "_k10", lambda ans, kept, removed: {"claim_id": "short_answer", "rule": "K10",
                                                                 "verdict": "pass"})
    ledger = answer_state.SummaryLedger()
    rows = [{"claim_id": "c2", "verdict": "cut", "reason": "unsupported", "rule": "critic"}]
    ask._critic_cuts(answer, rows, None, (date(2026, 9, 22), date(2026, 9, 28)), [], ledger=ledger)
    assert ledger.removals == [("recheck", "K6")]


# A-03b. A metadata fault never fails a paid Ask, and adds no call and no spend (A-08).
def test_a03b_a_build_that_raises_leaves_the_answer_and_run_and_logs(monkeypatch, caplog):
    normal_h = Harness(model=HeadlineModel())
    normal = normal_h.run()

    def boom(**kwargs):
        raise RuntimeError("build exploded")

    monkeypatch.setattr(answer_state, "build", boom)
    h = Harness(model=HeadlineModel())
    with caplog.at_level(logging.ERROR):
        out = h.run()
    assert "answer_meta" not in out
    run_ids = re.compile(r"r_\d{8}_\d{6}_\w+")  # each run stamps its own id into the numbers

    def same(a, b):
        return run_ids.sub("RUN", json.dumps(a, sort_keys=True)) == run_ids.sub("RUN", json.dumps(b, sort_keys=True))

    assert same(out["answer"], normal["answer"]) and out["run"]["model_usd"] == normal["run"]["model_usd"]
    assert schemas(h) == schemas(normal_h)
    assert any("answer_meta not built" in r.getMessage() for r in caplog.records)


def test_a03b_a_refusal_branch_build_that_raises_leaves_the_refused_answer(monkeypatch, caplog):
    monkeypatch.setattr(answer_state, "build", lambda **kw: (_ for _ in ()).throw(ValueError("bad")))
    with caplog.at_level(logging.ERROR):
        out = Harness(research=make_research(seen={}), spent=lambda: current_model_cap() + 5.0).run()
    assert "answer_meta" not in out and out["answer"]["status"] == "insufficient_evidence"
    assert any("answer_meta not built" in r.getMessage() for r in caplog.records)


# A-05. The model cannot set or name a state.
def test_a05_extra_keys_a_model_adds_never_reach_the_meta(monkeypatch):
    class Injecting(HeadlineModel):
        writer_out = {**WRITER_OUT, "answer_meta": {"summary": {"state": "shown"}}, "summary_state": "shown",
                      "execution": {"state": "failed"}}

    out = Harness(model=Injecting()).run()
    assert out["answer_meta"]["summary"]["state"] == "shown_rewritten"
    blob = json.dumps(out["answer_meta"])
    for text in ("summary_state", "failed", "Injecting"):
        assert text not in blob


def test_a05_a_check_that_returns_a_top_level_answer_meta_fails_the_contract():
    def hostile(draft, ctx, warehouse, window, markets):
        checked, verdicts = checks.check_answer(draft, ctx, warehouse, window=window, markets=markets)
        return {**checked, "answer_meta": {"x": 1}}, verdicts

    with pytest.raises(RuntimeError, match="fails the contract"):
        Harness(check=hostile).run()


def test_a05_every_string_in_a_serialised_meta_is_an_enum_member_an_id_a_digest_or_an_answer_status():
    out = Harness(model=HeadlineModel({"text": RISING_K6})).run()
    allowed = {*answer_state.EXECUTION_STATES, *answer_state.STOP_REASONS, *answer_state.SUMMARY_STATES,
               *answer_state.REMOVAL_STAGES, *answer_state.REMOVAL_CAUSES, *answer_state.REWRITE_OUTCOMES,
               *answer_state.ANSWER_STATUSES, ASK_ID, out["run"]["run_id"], "ask_id", "check_run_id", "execution",
               "state", "stop_reason", "summary", "removals", "rewrite", "bound", "answer_status", "summary_blank",
               "claims", "digest", "stage", "cause", "v"}

    def strings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    stray = [s for s in strings(out["answer_meta"]) if s not in allowed and not s.startswith("sha256:")]
    assert stray == []


# A-09. A gap round: the meta describes the pass that produced the stored answer.
def test_a09_a_gap_round_that_keeps_the_summary_describes_the_second_pass_only():
    model = RedraftedGapModel(("partial", "supported", "supported"))
    h = Harness(research=Lanes(parties=4), check=None, model=model)
    out = h.run(tier="T2")
    meta = out["answer_meta"]
    assert meta["summary"]["state"] == "shown" and meta["summary"]["removals"] == []
    assert answer_state.verify_stored(record_of(out)) is None


def test_a09_a_second_pass_that_also_loses_the_summary_and_leaves_one_claim_is_fixed_text_with_a_reset_ledger():
    model = RedraftedGapModel(("partial", "supported", "partial", "partial"))
    h = Harness(research=Lanes(parties=4), check=None, model=model)
    out = h.run(tier="T2")
    check(out, "completed", {"state": "fixed_text", "removals": [], "rewrite": "not_attempted"})
    assert out["answer"]["status"] == "insufficient_evidence" and out["answer"]["short_answer"] == checks.INSUFFICIENT


def test_a09_each_rewrite_reason_the_writer_defines_maps_to_its_outcome():
    assert ask._REWRITE_OUTCOMES == {
        writer.HEADLINE_REWRITTEN_REASON: "kept", writer.HEADLINE_USED_REASON: "used_up",
        writer.HEADLINE_BUDGET_REASON: "no_budget", writer.HEADLINE_FAILED_REASON: "call_failed",
        writer.HEADLINE_INPUT_REASON: "too_long", writer.HEADLINE_UNUSABLE_REASON: "empty",
        writer.HEADLINE_REPEATS_REASON: "repeated_removed_claim"}
    assert set(ask._REWRITE_OUTCOMES) == set(writer.HEADLINE_REASONS)


# A-14. A sweep over the paths: no unattributed cause, and the invariants the pairing rules rest on.
SWEEP = [
    (HeadlineModel, {}), (HeadlineModel, {"out": {"text": "  "}}), (HeadlineModel, {"out": {"verdict": "supported"}}),
    (HeadlineModel, {"out": {"text": RISING_K6}}), (HeadlineModel, {"out": {"text": RISING_K2}}),
    (HeadlineModel, {"forecast_headline": True}), (NarrowingModel, {}),
]


@pytest.mark.parametrize("cls, kwargs", SWEEP)
def test_a14_no_path_produces_an_unattributed_cause_and_the_invariants_hold(cls, kwargs):
    out = Harness(model=cls(**kwargs)).run()
    meta = out["answer_meta"]
    causes = [r["cause"] for r in meta["summary"]["removals"]]
    assert "unattributed" not in causes
    blank = out["answer"]["short_answer"].strip() == ""
    if blank:
        assert meta["summary"]["state"] in ("removed", "blank_unexplained")
    if meta["summary"]["state"] == "removed":
        assert out["answer"]["status"] == "partial" and meta["execution"]["state"] == "completed"
    if meta["summary"]["state"] == "shown":
        assert not blank
    assert answer_state.verify_stored(record_of(out)) is None
