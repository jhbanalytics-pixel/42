"""C1 6.5: the smoke judges the producer's typed summary state (W8-DEC-07 and the Q1 ruling).

core/agent/answer_state.py (lane 1) is not in this branch, so check_wire is a double (smoke_support.answer_state_double):
it answers None, or the problem code a test sets. Everything these tests pin is what the smoke decides on top of it:
which verified states may stand behind a blank summary, which gaps and fixed texts must go with them, and that a
missing, forged, contradicting or unreadable state fails.
"""
import json
import runpy
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from smoke_support import answer_state_double, with_wire, wire  # noqa: F401

from core.api import smoke

FIXTURE_ASK = Path(smoke.__file__).resolve().parent / "fixtures" / "ask_complete.json"
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"


def producer_gaps(*kinds):
    from core.agent import checks, plain, writer

    out = []
    for kind in kinds:
        if kind == "headline":
            out.append(dict(writer.HEADLINE_GAP))
        elif kind == "narrowed":
            out.append(dict(writer.NARROWED_HEADLINE_GAP))
        elif kind == "field_k6":
            out.append(plain.gap({"what": f"Short answer removed: {writer.DEMOGRAPHIC_WHAT}", "searched": "the short answer text",
                                  "why": f"field check: {writer.FIELD_WHY}"}))
        elif kind == "unchecked":
            out.append(plain.gap({"what": f"Short answer removed: {writer.FIELD_UNCHECKED_WHAT}", "searched": "the short answer text",
                                  "why": writer.FIELD_UNCHECKED_WHY}))
        else:
            out.append(plain.gap(checks._code_gap("Short answer removed", kind, "the short answer text")))
    return out


def stored(short_answer, answer_status, gaps, state):
    record = json.loads(FIXTURE_ASK.read_text(encoding="utf-8"))
    record["answer"].update(short_answer=short_answer, status=answer_status, gaps=gaps)
    return with_wire(record, state) if state is not None else record


def fixed_texts():
    from core.agent import ask, checks

    window = (date(2026, 10, 1), date(2026, 10, 7))
    return {"completed": checks.INSUFFICIENT,
            "stopped_on_request": ask._stopped_answer("2026-10-08", [], window)["short_answer"],
            "budget_full": ask.budget_stop_words(None)[1],
            "model_call_unverified": ask.budget_stop_words("usage_unknown")[1],
            "price_unreadable": ask.budget_stop_words("model_price_invalid")[1],
            "refused_budget_spent": ask._refused_answer("2026-10-08", window)["short_answer"]}


def removed(*removals, rewrite="not_attempted"):
    return wire("removed", removals=removals, rewrite=rewrite)


# id: (answer status, gap kinds, state, passes while ACCEPT_ANY_VERIFIED_REMOVAL is False, passes with it True)
BLANK_CASES = {
    "F10 retained K6": ("partial", ["K6"], removed(("first_check", "K6")), False, True),
    "F11 unpinned figure": ("partial", ["K2"], removed(("first_check", "K2")), False, True),
    "F12a cut claim, empty rewrite": ("partial", ["headline"], removed(("support_check", "claim_cut"), rewrite="empty"), True, True),
    "F12c cut claim, no budget": ("partial", ["headline"], removed(("support_check", "claim_cut"), rewrite="no_budget"), True, True),
    "F13 narrowed, rewrite repeats": ("partial", ["headline", "narrowed"],
                                      removed(("support_check", "claim_cut"), ("support_check", "claim_narrowed"),
                                              rewrite="repeated_removed_claim"), True, True),
    "F14 rewrite fails K6 on recheck": ("partial", ["headline", "K6"],
                                        removed(("support_check", "claim_cut"), ("recheck", "K6"), rewrite="removed_after_check"), True, True),
    "F15a rewrite flagged K9": ("partial", ["headline", "K9"],
                                removed(("support_check", "claim_cut"), ("field_check", "K9"), rewrite="removed_after_check"), True, True),
    "F15b draft flagged by the field check": ("partial", ["field_k6"], removed(("field_check", "K6")), False, True),
    "F16 too long to field check": ("partial", ["unchecked"], removed(("field_check", "field_unchecked")), False, True),
    "F17 critic cut": ("partial", ["headline"], removed(("support_check", "claim_cut"), ("critic", "claim_cut")), True, True),
    "F17 critic cut alone": ("partial", ["headline"], removed(("critic", "claim_cut")), True, True),
    "F18 model wrote a blank": ("complete", [], wire("blank_unexplained"), False, False),
}


@pytest.mark.parametrize("name", sorted(BLANK_CASES))
def test_s01_blank_summaries_are_judged_by_their_verified_removal_state(name, monkeypatch):
    status, gaps, state, interim, widened = BLANK_CASES[name]
    record = stored("", status, producer_gaps(*gaps), state)
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", False)
    ok, reason = smoke.check_ask_record(record)
    assert ok is interim, reason
    if not ok:
        assert "blank summary" in reason
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", True)
    ok, reason = smoke.check_ask_record(record)
    assert ok is widened, reason


def test_s01_the_widening_constant_is_on_because_w8_dec_07_was_typed_yes():
    assert smoke.ACCEPT_ANY_VERIFIED_REMOVAL is True


def test_s01_by_default_the_retained_f10_record_now_passes_with_its_verified_removal_state():
    # The one expectation W8-DEC-07 changes: a blank behind a verified first_check removal and its gap passes.
    record = stored("", "partial", producer_gaps("K6"), removed(("first_check", "K6")))
    assert smoke.check_ask_record(record)[0] is True


def test_s01_by_default_a_blank_with_no_state_or_a_forged_one_still_fails():
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("K6"), None))[0] is False
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("K6"), {"check": "unverified", "problem": "digest"}))[0] is False
    assert smoke.check_ask_record(stored("", "partial", [], removed(("first_check", "K6"))))[0] is False


def test_s01_the_original_a80_5_of_6_record_fails_the_ask_check_as_it_did_then(monkeypatch):
    # a_20261008_b1d10398: record complete, answer partial, blank summary, the K6 removal gap and no HEADLINE_GAP.
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", False)
    record = stored("", "partial", producer_gaps("K6"), removed(("first_check", "K6")))
    ok, reason = smoke.check_ask_record(record)
    assert ok is False and "blank summary" in reason


@pytest.mark.parametrize("kind", ["no_key", "legacy_unknown", "unverified", "other_check"])
def test_s01_a_record_without_a_verified_state_fails_whatever_its_summary(kind):
    state = {"legacy_unknown": {"check": "legacy_unknown"}, "unverified": {"check": "unverified", "problem": "digest"},
             "other_check": {"check": "trusted", "v": 1}}.get(kind)
    record = stored("The cited posts support this summary.", "complete", [], state)
    ok, reason = smoke.check_ask_record(record)
    assert ok is False and "no verified answer state" in reason
    if state is None:
        assert "no answer_meta" in reason
    else:
        assert state["check"] in reason or state.get("problem", "") in reason


def test_s01_f19_and_f20_carry_no_state_so_the_a80_headline_gap_alone_no_longer_passes():
    f19 = stored("", "partial", producer_gaps("K6"), None)
    f20 = stored("", "partial", producer_gaps("headline"), None)
    assert smoke.check_ask_record(f19)[0] is False
    ok, reason = smoke.check_ask_record(f20)
    assert ok is False and "no verified answer state" in reason


def test_s01_a_state_check_wire_rejects_fails_with_the_problem_code(answer_state_double):
    answer_state_double.problem = "digest"
    ok, reason = smoke.check_ask_record(stored("A summary.", "complete", [], wire()))
    assert ok is False and "digest" in reason and "no verified answer state" in reason


def test_s01_without_the_producer_module_the_check_fails_closed(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.agent.answer_state", None)
    ok, reason = smoke.check_ask_record(stored("A summary.", "complete", [], wire()))
    assert ok is False and "no verified answer state" in reason


def test_s01_check_wire_gets_the_record_exactly_as_the_api_returned_it(answer_state_double):
    record = stored("A summary.", "complete", [], wire())
    smoke.check_ask_record(record)
    assert answer_state_double.records == [record]


@pytest.mark.parametrize("summary, status, state", [
    ("", "partial", wire("shown")),
    ("", "partial", wire("shown_rewritten", removals=[("support_check", "claim_cut")], rewrite="kept")),
    ("A summary.", "partial", removed(("support_check", "claim_cut"))),
    ("A summary.", "complete", wire("blank_unexplained")),
    ("A summary.", "complete", wire("no_answer")),
    ("", "complete", removed(("support_check", "claim_cut"))),
    ("", "insufficient_evidence", removed(("support_check", "claim_cut"))),
    ("", "partial", wire("removed", removals=[("support_check", "claim_cut")], execution="stopped_on_request")),
    ("", "partial", wire("removed", removals=[], rewrite="not_attempted")),
])
def test_s01_a_summary_that_contradicts_its_state_fails(summary, status, state):
    ok, reason = smoke.check_ask_record(stored(summary, status, producer_gaps("headline"), state))
    assert ok is False, reason


@pytest.mark.parametrize("flag", [False, True])
def test_s01_unattributed_and_blank_unexplained_never_pass_in_either_setting(flag, monkeypatch):
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", flag)
    unattributed = removed(("first_check", "unattributed"), ("support_check", "claim_cut"))
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), unattributed))[0] is False
    assert smoke.check_ask_record(stored("", "partial", [], wire("blank_unexplained")))[0] is False


def test_s02_a_blank_with_the_exact_headline_gap_and_no_state_fails_and_with_a_support_removal_passes():
    gaps = producer_gaps("headline")
    assert smoke.check_ask_record(stored("", "partial", gaps, None))[0] is False
    assert smoke.check_ask_record(stored("", "partial", gaps, removed(("support_check", "claim_cut"))))[0] is True


def test_s03_the_expected_gap_of_every_removal_must_be_present():
    state = removed(("support_check", "claim_cut"), ("support_check", "claim_narrowed"), rewrite="repeated_removed_claim")
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline", "narrowed"), state))[0] is True
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), state))[0] is False
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("narrowed"), state))[0] is False
    state = removed(("support_check", "claim_cut"), ("recheck", "K6"), rewrite="removed_after_check")
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), state))[0] is False
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline", "K2"), state))[0] is False
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline", "K6"), state))[0] is True


def test_s03_a_shown_summary_needs_no_removal_gap_and_a_critic_state_needs_no_narrowed_gap():
    kept = wire("shown_rewritten", removals=[("support_check", "claim_cut")], rewrite="kept")
    assert smoke.check_ask_record(stored("A summary.", "partial", [], kept))[0] is True
    kept = wire("shown_rewritten", removals=[("support_check", "claim_narrowed")], rewrite="kept")
    assert smoke.check_ask_record(stored("A summary.", "complete", [], kept))[0] is True
    critic = removed(("support_check", "claim_narrowed"), ("critic", "claim_cut"))
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), critic))[0] is True


def test_s03_gap_texts_are_compared_by_what_and_why_and_searched_may_differ(monkeypatch):
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", True)
    gap = producer_gaps("K3")[0]
    gap["searched"] = "text the producer varies for this rule"
    state = removed(("first_check", "K3"))
    assert smoke.check_ask_record(stored("", "partial", [gap], state))[0] is True
    assert smoke.check_ask_record(stored("", "partial", [{**gap, "why": gap["why"] + " changed"}], state))[0] is False
    assert smoke.check_ask_record(stored("", "partial", [{**gap, "what": gap["what"] + " changed"}], state))[0] is False


def test_s03_fixed_texts_are_bound_to_the_execution_state_that_writes_them():
    texts = fixed_texts()

    def fixed(text, execution, stop_reason=None):
        state = wire("fixed_text", execution=execution, stop_reason=stop_reason)
        return smoke.check_ask_record(stored(text, "insufficient_evidence", [], state))[0]

    assert fixed(texts["completed"], "completed") is True
    assert fixed(texts["stopped_on_request"], "stopped_on_request") is True
    assert fixed(texts["budget_full"], "stopped_on_budget", "budget_full") is True
    assert fixed(texts["model_call_unverified"], "stopped_on_budget", "model_call_unverified") is True
    assert fixed(texts["price_unreadable"], "stopped_on_budget", "price_unreadable") is True
    assert fixed(texts["refused_budget_spent"], "refused_budget_spent") is True
    assert len({texts[k] for k in texts}) == len(texts), "the six fixed texts must differ for the binding to mean anything"
    # a text from one state does not stand in for another
    assert fixed(texts["budget_full"], "completed") is False
    assert fixed(texts["completed"], "stopped_on_request") is False
    assert fixed(texts["budget_full"], "stopped_on_budget", "model_call_unverified") is False
    assert fixed(texts["model_call_unverified"], "stopped_on_budget", "budget_full") is False
    assert fixed(texts["refused_budget_spent"], "stopped_on_budget", "budget_full") is False
    assert fixed(texts["completed"], "refused_budget_spent") is False
    # one changed character
    for name, execution, reason in (("completed", "completed", None), ("budget_full", "stopped_on_budget", "budget_full")):
        assert fixed(texts[name] + ".", execution, reason) is False
        assert fixed(texts[name][:-1] + "!", execution, reason) is False


def test_s03_a_fixed_text_summary_state_with_any_other_text_fails():
    state = wire("fixed_text", execution="completed")
    assert smoke.check_ask_record(stored("A summary the model wrote.", "insufficient_evidence", [], state))[0] is False


def test_s08_the_candidate_accepts_no_blank_record_a80_refuses_and_exactly_the_verified_removals_a80_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", False)  # the setting the comparison is stated for
    a80 = subprocess.run(["git", "show", f"{A80}:core/api/smoke.py"], capture_output=True, cwd=Path(__file__).resolve().parents[3])
    if a80.returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout; a shallow clone cannot run the release tests")
    path = tmp_path / "smoke_a80.py"
    path.write_bytes(a80.stdout)
    old = runpy.run_path(str(path))["check_ask_record"]
    for name, (status, gaps, state, interim, widened) in BLANK_CASES.items():
        record = stored("", status, producer_gaps(*gaps), state)
        accepted_then = old(json.loads(json.dumps(record)))[0]
        assert accepted_then == ("headline" in gaps and status == "partial"), name
        new = smoke.check_ask_record(record)[0]
        assert not (new and not accepted_then), f"{name}: the candidate accepts a blank the a80 smoke refuses"
        has_support_or_critic = any(r["stage"] in ("support_check", "critic") for r in state["summary"]["removals"])
        assert new == (accepted_then and has_support_or_critic), name
    for stateless in (stored("", "partial", producer_gaps("headline"), None), stored("", "partial", producer_gaps("K6"), None)):
        assert not (smoke.check_ask_record(stateless)[0] and not old(stateless)[0])


def test_s02_the_standalone_runner_loads_the_state_check_outside_the_repository(tmp_path):
    record = stored("", "complete", producer_gaps("headline"), removed(("support_check", "claim_cut")))
    code = ("import json, runpy, sys, types; module = runpy.run_path(sys.argv[1]); "
            "sys.modules['core.agent.answer_state'] = types.SimpleNamespace(check_wire=lambda record: None); "
            "record = json.loads(sys.argv[2]); "
            "assert module['check_ask_record'](record)[0] is False; "
            "record['answer']['status'] = 'partial'; "
            "assert module['check_ask_record'](record)[0] is True; "
            "record.pop('answer_meta'); "
            "assert module['check_ask_record'](record)[0] is False")
    proc = subprocess.run([sys.executable, "-c", code, str(Path(smoke.__file__)), json.dumps(record)], cwd=tmp_path,
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8")


def test_s03_a_shown_summary_needs_a_complete_or_partial_answer_and_a_fixed_text_needs_insufficient_evidence():
    assert smoke.check_ask_record(stored("A summary.", "insufficient_evidence", [], wire()))[0] is False
    fixed = fixed_texts()["completed"]
    assert smoke.check_ask_record(stored(fixed, "complete", [], wire("fixed_text")))[0] is False
    assert smoke.check_ask_record(stored(fixed, "partial", [], wire("fixed_text")))[0] is False
    assert smoke.check_ask_record(stored(fixed, "insufficient_evidence", [], wire("fixed_text")))[0] is True


def test_s03_a_malformed_state_body_fails_instead_of_raising():
    for state in ({"check": "verified", "v": 1}, {"check": "verified", "v": 1, "execution": {}, "summary": {}},
                  {"check": "verified", "v": 1, "execution": {"state": "completed"}, "summary": {"state": "shown", "removals": [1]}}):
        ok, reason = smoke.check_ask_record(stored("A summary.", "complete", [], state))
        assert ok is False and "no verified answer state" in reason


@pytest.mark.parametrize("flag", [False, True])
def test_s01_a_removed_state_with_no_removals_or_a_pair_the_producer_never_writes_never_passes(flag, monkeypatch):
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", flag)
    gaps = producer_gaps("headline", "K6")
    assert smoke.check_ask_record(stored("", "partial", gaps, wire("removed", removals=[])))[0] is False
    for pair in (("critic", "K6"), ("first_check", "claim_cut"), ("support_check", "K6"), ("field_check", "claim_cut"),
                 ("recheck", "claim_narrowed"), ("somewhere", "K6")):
        assert smoke.check_ask_record(stored("", "partial", gaps, removed(pair)))[0] is False, pair


def test_s01_with_the_interim_rule_a_removal_at_no_support_or_critic_stage_fails_even_when_the_headline_gap_is_present(monkeypatch):
    monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", False)
    gaps = producer_gaps("K6", "headline")
    assert smoke.check_ask_record(stored("", "partial", gaps, removed(("first_check", "K6"))))[0] is False


def test_s03_a_critic_state_expects_no_narrowed_gap_but_a_state_without_a_critic_does():
    narrowed = removed(("support_check", "claim_narrowed"))
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), narrowed))[0] is False
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline", "narrowed"), narrowed))[0] is True
    with_critic = removed(("support_check", "claim_narrowed"), ("critic", "claim_cut"))
    assert smoke.check_ask_record(stored("", "partial", producer_gaps("headline"), with_critic))[0] is True


@pytest.mark.parametrize("name", sorted(n for n, case in BLANK_CASES.items() if case[4]))
def test_s03_every_expected_gap_of_every_accepted_blank_is_needed_in_either_setting(name, monkeypatch):
    status, gaps, state, interim, widened = BLANK_CASES[name]
    for flag in (False, True):
        monkeypatch.setattr(smoke, "ACCEPT_ANY_VERIFIED_REMOVAL", flag)
        assert smoke.check_ask_record(stored("", status, producer_gaps(*gaps), state))[0] is (interim if not flag else widened)
        for index in range(len(gaps)):
            fewer = [g for i, g in enumerate(gaps) if i != index]
            assert smoke.check_ask_record(stored("", status, producer_gaps(*fewer), state))[0] is False, (name, gaps[index], flag)
        if not gaps:
            continue
        assert smoke.check_ask_record(stored("", status, [], state))[0] is False
