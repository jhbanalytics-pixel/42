"""The answers and typed summary states the fixture agent returns when F42_FIXTURE_STATE names a fixture of C1 v2
section 7 (P-19), so a journey or the release smoke can be run against any producer state without a model.

Each answer is made from the producer's own constants (the gaps, the fixed texts), and each state by
core.agent.answer_state.build, so what comes out is what the pipeline would have stored for that path. The ids are
those of the catalogue in C1 v2 section 7. F05 (a stop request) needs the stop to arrive from outside, F09 is the
fixture agent failing on purpose, and F19 and F20 are records from before the field existed, so those three return
no state."""
import copy
from datetime import date

from core.agent import answer_state

WINDOW = (date(2026, 9, 21), date(2026, 9, 27))
FAILS = "F09"
NO_STATE = ("F19", "F20")

# id: (execution state, stop reason, summary state, removals, rewrite, how the answer is made)
SPECS = {
    "F01": ("completed", None, "shown", [], "not_attempted", "complete"),
    "F02": ("completed", None, "shown_rewritten", [("support_check", "claim_cut")], "kept", "partial"),
    "F03": ("completed", None, "shown_rewritten", [("support_check", "claim_narrowed")], "kept", "complete"),
    "F04": ("completed", None, "fixed_text", [], "not_attempted", "insufficient"),
    "F06": ("stopped_on_budget", "budget_full", "fixed_text", [], "not_attempted", "stop:budget_full"),
    "F07": ("stopped_on_budget", "model_call_unverified", "fixed_text", [], "not_attempted",
            "stop:model_call_unverified"),
    "F08": ("refused_budget_spent", None, "fixed_text", [], "not_attempted", "refused"),
    "F10": ("completed", None, "removed", [("first_check", "K6")], "not_attempted", "blank"),
    "F11": ("completed", None, "removed", [("first_check", "K2")], "not_attempted", "blank"),
    "F12a": ("completed", None, "removed", [("support_check", "claim_cut")], "empty", "blank"),
    "F12b": ("completed", None, "removed", [("support_check", "claim_cut")], "call_failed", "blank"),
    "F12c": ("completed", None, "removed", [("support_check", "claim_cut")], "no_budget", "blank"),
    "F13": ("completed", None, "removed", [("support_check", "claim_cut"), ("support_check", "claim_narrowed")],
            "repeated_removed_claim", "blank"),
    "F14": ("completed", None, "removed", [("support_check", "claim_cut"), ("recheck", "K6")], "removed_after_check",
            "blank"),
    "F15a": ("completed", None, "removed", [("support_check", "claim_cut"), ("field_check", "K9")],
             "removed_after_check", "blank"),
    "F15b": ("completed", None, "removed", [("field_check", "K6")], "not_attempted", "blank"),
    "F16": ("completed", None, "removed", [("field_check", "field_unchecked")], "not_attempted", "blank"),
    "F17": ("completed", None, "removed", [("support_check", "claim_cut"), ("critic", "claim_cut")],
            "removed_after_check", "blank"),
    "F18": ("completed", None, "blank_unexplained", [], "not_attempted", "unexplained"),
}
STOP_REASONS = {"budget_full": None, "model_call_unverified": "usage_unknown"}


def ids():
    return sorted(set(SPECS) | {FAILS, *NO_STATE, "F05"})


def removal_gaps(removals):
    """The gap each removal leaves in the stored answer (C1 2.5), from the producer's constants."""
    from core.agent import checks, plain, writer

    critic = any(stage == "critic" for stage, _ in removals)
    gaps = []
    for stage, cause in removals:
        if stage in ("first_check", "recheck") or (stage == "field_check" and cause in ("K3", "K9")):
            gaps.append(plain.gap(checks._code_gap("Short answer removed", cause, "the short answer text")))
        elif stage == "field_check" and cause == "K6":
            gaps.append(plain.gap({"what": f"Short answer removed: {writer.DEMOGRAPHIC_WHAT}",
                                   "searched": "the short answer text", "why": f"field check: {writer.FIELD_WHY}"}))
        elif stage == "field_check":
            gaps.append(plain.gap({"what": f"Short answer removed: {writer.FIELD_UNCHECKED_WHAT}",
                                   "searched": "the short answer text", "why": writer.FIELD_UNCHECKED_WHY}))
        elif stage == "support_check" and not critic:
            gaps.append(dict(writer.HEADLINE_GAP))
            if cause == "claim_narrowed":
                gaps.append(dict(writer.NARROWED_HEADLINE_GAP))
        elif stage == "critic":
            gaps.append(dict(writer.HEADLINE_GAP))
    unique = []
    for gap in gaps:
        if gap not in unique:
            unique.append(gap)
    return unique


def _answer(kind, base):
    from core.agent import ask, checks

    answer = copy.deepcopy(base)
    as_of = answer["as_of"]
    if kind == "complete":
        answer["status"] = "complete"
    elif kind == "partial":
        answer["status"] = "partial"
    elif kind == "insufficient":
        answer.update(status="insufficient_evidence", claims=[], so_what=[], watch_next=[], evidence=[],
                      short_answer=checks.INSUFFICIENT)
    elif kind.startswith("stop:"):
        reason = kind.split(":", 1)[1]
        answer = ask._stopped_answer(as_of, [], WINDOW, budget_stopped=True, reason=STOP_REASONS[reason])
    elif kind == "refused":
        answer = ask._refused_answer(as_of, WINDOW)
    elif kind == "unexplained":
        answer.update(status="complete", short_answer="")
    return answer


def build(fixture_id, request, base, run):
    """(answer, answer_meta or None) for the fixture. ValueError for an id that cannot be served."""
    if fixture_id in NO_STATE:
        answer = copy.deepcopy(base)
        if fixture_id == "F19":  # the retained case as an a80 agent stored it: blank, partial, the K6 gap, no state
            answer.update(status="partial", short_answer="", gaps=answer["gaps"] + removal_gaps([("first_check", "K6")]))
        else:  # the older record with the exact HEADLINE_GAP and no key
            answer.update(status="partial", short_answer="", gaps=answer["gaps"] + removal_gaps(
                [("support_check", "claim_cut")]))
        return answer, None
    if fixture_id not in SPECS:
        raise ValueError(f"F42_FIXTURE_STATE {fixture_id!r} is not a fixture this agent can serve: "
                         + ", ".join(ids()))
    execution, stop_reason, summary, removals, rewrite, kind = SPECS[fixture_id]
    answer = _answer(kind, base)
    if summary == "removed":
        answer.update(status="partial", short_answer="", gaps=answer["gaps"] + removal_gaps(removals))
    meta = answer_state.build(ask_id=request["ask_id"], check_run_id=run.get("run_id"), execution_state=execution,
                              stop_reason=stop_reason, summary_state=summary, removals=removals, rewrite=rewrite,
                              answer=answer)
    return answer, meta
