"""C1 (3.4a): the typed summary state. The module core/agent/answer_state.py, tests A-01 to A-05, A-12 and A-13, the
forged, missing and conflicting series (N) and the wire check (A-04b).

Every expectation is written here. The independent digest function below is not the module's: a digest the module
computes is only proof if something else recomputes it (the defect class this contract exists for).
"""

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from core.agent import answer_state as st

ASK_ID, RUN_ID = "a_20261008_b1d10398", "r_20261008_093150_b8216971_7cf0bce237cb4e4288a43152462e8964"


def digest_of(meta):
    body = {k: v for k, v in meta.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def answer(status="partial", summary="", claims=4):
    return {"status": status, "as_of": "2026-10-08", "short_answer": summary,
            "claims": [{"id": f"c{i}"} for i in range(1, claims + 1)], "evidence": [], "so_what": [], "watch_next": [],
            "gaps": [{"what": "x", "searched": "y", "why": "z"}], "context": ""}


def make(execution="completed", stop=None, summary="removed", removals=(("first_check", "K6"),),
         rewrite="not_attempted", ans=None):
    ans = answer() if ans is None else ans
    return st.build(ask_id=ASK_ID, check_run_id=RUN_ID, execution_state=execution, stop_reason=stop,
                    summary_state=summary, removals=list(removals), rewrite=rewrite, answer=ans)


def record(meta, ans=None, status="complete", run_id=RUN_ID, ask_id=ASK_ID):
    return {"ask_id": ask_id, "status": status, "answer": answer() if ans is None else ans,
            "run": {"run_id": run_id}, "answer_meta": meta}


def forge(meta, **edits):
    """Edit fields by dotted path, then recompute the digest, so only the content is wrong."""
    out = json.loads(json.dumps(meta))
    for path, value in edits.items():
        node = out
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[int(keys[-1]) if isinstance(node, list) else keys[-1]] = value
    if "digest" in out:
        out["digest"] = digest_of(out)
    return out


# A-01. The enums and the stage to cause table are exactly the contract's.
def test_a01_the_closed_enums_and_the_stage_cause_table():
    assert st.EXECUTION_STATES == ("completed", "stopped_on_request", "stopped_on_budget", "refused_budget_spent",
                                   "failed")
    assert st.STOP_REASONS == ("budget_full", "model_call_unverified", "price_unreadable")
    assert st.SUMMARY_STATES == ("shown", "shown_rewritten", "fixed_text", "removed", "blank_unexplained", "no_answer")
    assert st.REMOVAL_STAGES == ("first_check", "support_check", "recheck", "field_check", "critic")
    assert st.REMOVAL_CAUSES == ("K2", "K3", "K6", "K8", "K9", "claim_cut", "claim_narrowed", "field_unchecked",
                                 "unattributed")
    assert st.REWRITE_OUTCOMES == ("not_attempted", "kept", "removed_after_check", "used_up", "no_budget",
                                   "call_failed", "too_long", "empty", "repeated_removed_claim")
    assert st.STAGE_CAUSES == {
        "first_check": ("K2", "K3", "K6", "K8", "K9"), "recheck": ("K2", "K3", "K6", "K8", "K9"),
        "support_check": ("claim_cut", "claim_narrowed"), "field_check": ("K3", "K6", "K9", "field_unchecked"),
        "critic": ("claim_cut",)}
    assert st.VERSION == 1


# A-02. The digest, recomputed by something else.
def test_a02_the_digest_is_the_sha256_of_the_canonical_body_and_every_field_changes_it():
    meta = make()
    assert meta["digest"] == digest_of(meta)
    assert set(meta) == {"v", "ask_id", "check_run_id", "execution", "summary", "bound", "digest"}
    assert meta["bound"] == {"answer_status": "partial", "summary_blank": True, "claims": 4}
    assert meta["summary"] == {"state": "removed", "removals": [{"stage": "first_check", "cause": "K6"}],
                               "rewrite": "not_attempted"}
    assert meta["execution"] == {"state": "completed", "stop_reason": None}
    edits = {"v": 2, "ask_id": "a_other", "check_run_id": "r_other", "execution__state": "failed",
             "execution__stop_reason": "budget_full", "summary__state": "shown", "summary__rewrite": "kept",
             "summary__removals__0__cause": "K2", "summary__removals__0__stage": "recheck",
             "bound__answer_status": "complete", "bound__summary_blank": False, "bound__claims": 5}
    for path, value in edits.items():
        changed = json.loads(json.dumps(meta))
        node = changed
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[int(keys[-1]) if isinstance(node, list) else keys[-1]] = value
        assert digest_of(changed) != meta["digest"], path


# A-03. build refuses what the contract forbids.
@pytest.mark.parametrize("kwargs", [
    dict(summary="mystery"), dict(execution="paused"), dict(rewrite="maybe"),
    dict(removals=(("first_check", "claim_cut"),)), dict(removals=(("critic", "K6"),)),
    dict(removals=(("nowhere", "K6"),)),
    dict(stop="budget_full"),  # a stop reason with `completed`
    dict(execution="stopped_on_budget", stop=None, summary="fixed_text", removals=()),
    dict(execution="stopped_on_budget", stop="nonsense", summary="fixed_text", removals=()),
    dict(removals=(("first_check", "K6"), ("first_check", "K6"))),  # a duplicate pair
    dict(removals=(("first_check", "K9"), ("first_check", "K2"))),  # out of enum order
])
def test_a03_build_refuses_what_the_contract_forbids(kwargs):
    with pytest.raises(ValueError):
        make(**kwargs)


def test_a03_unattributed_is_legal_in_any_stage_and_a_stop_reason_with_a_budget_stop_is_too():
    make(removals=(("support_check", "unattributed"),))
    make(execution="stopped_on_budget", stop="price_unreadable", summary="fixed_text", removals=(),
         ans=answer("insufficient_evidence", "stopped", 0))


def test_a03_build_failed_has_no_answer_and_all_bound_null():
    meta = st.build_failed(ask_id=ASK_ID, check_run_id=None)
    assert meta["execution"] == {"state": "failed", "stop_reason": None}
    assert meta["summary"] == {"state": "no_answer", "removals": [], "rewrite": "not_attempted"}
    assert meta["bound"] == {"answer_status": None, "summary_blank": None, "claims": None}
    assert meta["check_run_id"] is None and meta["digest"] == digest_of(meta)
    failed = {"ask_id": ASK_ID, "status": "failed", "answer": None, "run": {"run_id": None}, "answer_meta": meta}
    assert st.verify_stored(failed) is None


# The fixtures the N series is generated from (F10 and F01), plus the other execution states.
F10 = make()
F01 = make(summary="shown", removals=(), rewrite="not_attempted", ans=answer("complete", "A fine summary.", 4))
REC10, REC01 = record(F10), record(F01, answer("complete", "A fine summary.", 4))


FIXTURES = {
    "F01": REC01,
    "F02": record(make(summary="shown_rewritten", removals=(("support_check", "claim_cut"),), rewrite="kept",
                       ans=answer("partial", "Rewritten summary.", 3)), answer("partial", "Rewritten summary.", 3)),
    "F04": record(make(summary="fixed_text", removals=(), ans=answer("insufficient_evidence", "Not enough.", 1)),
                  answer("insufficient_evidence", "Not enough.", 1)),
    "F05": record(make(execution="stopped_on_request", summary="fixed_text", removals=(),
                       ans=answer("insufficient_evidence", "Stopped.", 0)),
                  answer("insufficient_evidence", "Stopped.", 0), status="stopped"),
    "F06": record(make(execution="stopped_on_budget", stop="budget_full", summary="fixed_text", removals=(),
                       ans=answer("insufficient_evidence", "Budget.", 0)), answer("insufficient_evidence", "Budget.", 0)),
    "F07": record(make(execution="stopped_on_budget", stop="model_call_unverified", summary="fixed_text", removals=(),
                       ans=answer("insufficient_evidence", "Failed.", 0)), answer("insufficient_evidence", "Failed.", 0)),
    "F08": record(make(execution="refused_budget_spent", summary="fixed_text", removals=(),
                       ans=answer("insufficient_evidence", "Refused.", 0)), answer("insufficient_evidence", "Refused.", 0)),
    "F09": {"ask_id": ASK_ID, "status": "failed", "answer": None, "run": {"run_id": None},
            "answer_meta": st.build_failed(ask_id=ASK_ID, check_run_id=None)},
    "F10": REC10,
    "F12a": record(make(removals=(("support_check", "claim_cut"),), rewrite="empty"), answer("partial", "", 3)),
    "F14": record(make(removals=(("support_check", "claim_cut"), ("recheck", "K6")), rewrite="removed_after_check")),
    "F15b": record(make(removals=(("field_check", "K6"),))),
    "F16": record(make(removals=(("field_check", "field_unchecked"),))),
    "F17": record(make(removals=(("support_check", "claim_cut"), ("critic", "claim_cut")),
                       rewrite="removed_after_check")),
    "F18": record(make(summary="blank_unexplained", removals=(), ans=answer("complete", "", 4)),
                  answer("complete", "", 4)),
}
FIXTURES["F12a"]["answer"] = answer("partial", "", 3)
FIXTURES["F12a"]["answer_meta"] = make(removals=(("support_check", "claim_cut"),), rewrite="empty",
                                       ans=answer("partial", "", 3))


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_every_fixture_stored_state_verifies(name):
    assert st.verify_stored(FIXTURES[name]) is None


# A-04. The N series: forged, missing and conflicting.
def n(meta_edits, rec=None, **record_edits):
    base = rec if rec is not None else REC10
    meta = forge(base["answer_meta"], **meta_edits)
    out = {**base, "answer_meta": meta, **record_edits}
    return out


def raw(meta, rec=REC10):
    return {**rec, "answer_meta": meta}


def alone(status="complete", text="A summary.", n_claims=3, **kw):
    """A record built so that exactly one clause is violated: the answer is partial, non-blank and has claims."""
    ans = answer("partial", text, n_claims)
    return record(make(ans=ans, **kw), ans, status=status)


N = [
    # shape
    ("N-S1", lambda: raw({**F10, "extra": 1}), "shape"),
    ("N-S2", lambda: raw({k: v for k, v in F10.items() if k != "bound"}), "shape"),
    ("N-S3", lambda: n({"v": "1"}), "shape"),
    ("N-S4", lambda: n({"v": True}), "shape"),
    ("N-S5", lambda: raw({**F10, "check": "verified"}), "forbidden_key"),
    ("N-S6", lambda: raw("x"), "shape"),
    ("N-S7", lambda: raw({**F10, "summary": {**F10["summary"], "extra": 1}}), "shape"),
    # enum
    ("N-E1", lambda: n({"summary__state": "weird"}), "enum"),
    ("N-E2", lambda: n({"summary__removals__0__stage": "elsewhere"}), "enum"),
    ("N-E3", lambda: n({"summary__rewrite": "perhaps"}), "enum"),
    ("N-E4", lambda: n({"execution__state": "paused"}), "enum"),
    # version
    ("N-V1", lambda: n({"v": 2}), "version"),
    # digest
    ("N-D1", lambda: raw({**F10, "digest": "sha256:" + "ab" * 32}), "digest"),
    ("N-D2", lambda: raw({**F10, "bound": {**F10["bound"], "claims": 5}}), "digest"),
    ("N-D3", lambda: raw({**F10, "digest": "nonsense"}), "digest"),
    # ids
    ("N-I1", lambda: n({"ask_id": "a_other"}), "ids"),
    ("N-I2", lambda: n({"check_run_id": "r_other"}), "ids"),
    # bound
    ("N-B1", lambda: n({"bound__summary_blank": False}), "bound"),
    ("N-B2", lambda: n({"bound__claims": 9}), "bound"),
    ("N-B3", lambda: n({"bound__answer_status": "complete"}), "bound"),
    # running
    ("N-R1", lambda: {**REC10, "status": "running"}, "while_running"),
    # P1
    ("N-P1a", lambda: {**REC10, "answer_meta": FIXTURES["F09"]["answer_meta"], "answer": None, "status": "complete"},
     "pairing"),
    ("N-P1b", lambda: {**FIXTURES["F09"], "answer": answer()}, "bound"),
    ("N-P1c", lambda: raw(forge(FIXTURES["F09"]["answer_meta"], summary__state="shown"), FIXTURES["F09"]), "pairing"),
    ("N-P1d", lambda: raw(forge(FIXTURES["F09"]["answer_meta"], bound__claims=1), FIXTURES["F09"]), "bound"),
    # P2
    ("N-P2a", lambda: {**REC10, "status": "failed"}, "pairing"),
    ("N-P2b", lambda: {**REC10, "answer": None}, "bound"),
    ("N-P2c", lambda: n({"summary__state": "no_answer"}), "pairing"),
    # P4
    ("N-P4a", lambda: {**FIXTURES["F05"], "status": "complete"}, "pairing"),
    ("N-P4b", lambda: n({"summary__state": "removed"}, FIXTURES["F05"]), "pairing"),
    ("N-P4c", lambda: alone(status="stopped", execution="stopped_on_request", summary="shown", removals=()), "pairing"),
    # P5
    ("N-P5a", lambda: n({"execution__stop_reason": None}, FIXTURES["F06"]), "pairing"),
    ("N-P5d", lambda: n({"execution__stop_reason": "budget_full"}, REC10), "pairing"),
    ("N-P5e", lambda: n({"execution__stop_reason": "budget_full"}, FIXTURES["F05"]), "pairing"),
    ("N-P5c", lambda: alone(execution="stopped_on_budget", stop="budget_full", summary="shown", removals=()),
     "pairing"),
    # P6
    ("N-P6b", lambda: alone(execution="refused_budget_spent", summary="shown", removals=()), "pairing"),
    # P7 to P11, the status, blank and execution clauses
    ("N-P7a", lambda: alone(text="", summary="shown", removals=()), "pairing"),
    ("N-P8a", lambda: alone(text="", summary="shown_rewritten", removals=(("support_check", "claim_cut"),),
                            rewrite="kept"), "pairing"),
    ("N-P9b", lambda: record(make(summary="fixed_text", removals=(), ans=answer("complete", "Fixed.", 2)),
                             answer("complete", "Fixed.", 2)), "pairing"),
    ("N-P10a", lambda: alone(summary="removed", removals=(("first_check", "K6"),)), "pairing"),
    ("N-P11a", lambda: alone(summary="blank_unexplained", removals=()), "pairing"),
    ("N-P7c", lambda: n({"summary__rewrite": "kept"}, REC01), "reasons"),
    ("N-P7d", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K6"}]}, REC01), "reasons"),
    # P8
    ("N-P8c", lambda: n({"summary__rewrite": "not_attempted"}, FIXTURES["F02"]), "reasons"),
    ("N-P8d", lambda: n({"summary__removals": []}, FIXTURES["F02"]), "reasons"),
    # P9
    ("N-P9c", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K6"}]}, FIXTURES["F04"]), "reasons"),
    ("N-P9d", lambda: n({"summary__rewrite": "kept"}, FIXTURES["F04"]), "reasons"),
    # P10
    ("N-P10c", lambda: n({"execution__state": "stopped_on_request"}, REC10), "pairing"),
    ("N-P10d", lambda: n({"summary__removals": []}, REC10), "reasons"),
    # P11
    ("N-P11c", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K6"}]}, FIXTURES["F18"]), "reasons"),
    ("N-P11d", lambda: n({"summary__rewrite": "empty"}, FIXTURES["F18"]), "reasons"),
    # P12
    ("N-P12a", lambda: n({"summary__state": "no_answer"}, REC10), "pairing"),
    ("N-P12b", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K6"}]}, FIXTURES["F09"]), "reasons"),
    # R1
    ("N-R1a", lambda: n({"summary__removals": [{"stage": "critic", "cause": "K6"}]}, REC10), "reasons"),
    ("N-R1b", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K6"},
                                               {"stage": "first_check", "cause": "K6"}]}, REC10), "reasons"),
    ("N-R1c", lambda: n({"summary__removals": [{"stage": "first_check", "cause": "K9"},
                                               {"stage": "first_check", "cause": "K2"}]}, REC10), "reasons"),
    # R2
    ("N-R2a", lambda: n({"summary__rewrite": "kept"}, REC10), "reasons"),
    ("N-R2b", lambda: n({"summary__rewrite": "removed_after_check"}, FIXTURES["F12a"]), "reasons"),
    ("N-R2c", lambda: n({"summary__rewrite": "removed_after_check"}, REC01), "reasons"),
    ("N-R2d", lambda: n({"summary__rewrite": "call_failed"}, REC10), "reasons"),
    ("N-R2e", lambda: n({"summary__rewrite": "used_up"}, FIXTURES["F02"]), "reasons"),
]


def run_n():
    return {name: st.verify_stored(build()) for name, build, _ in N}


@pytest.mark.parametrize("name, build, code", N)
def test_a04_the_n_series_reports_the_contracted_code(name, build, code):
    assert st.verify_stored(build()) == code, name


def test_a04_the_order_of_steps_holds_an_object_failing_shape_and_digest_reports_shape():
    assert st.verify_stored(raw({**F10, "extra": 1, "digest": "sha256:" + "00" * 32})) == "shape"


def test_a04_a_wire_looking_value_stored_is_forbidden_not_verified():
    wire = st.meta_view(REC10)
    assert wire["check"] == "verified"
    assert st.verify_stored(raw(wire)) == "forbidden_key"


# meta_view: steps 1 and 2 and the wire shapes.
def test_meta_view_returns_null_running_legacy_unknown_unverified_and_verified():
    assert st.meta_view({**REC10, "status": "running", "answer": None, "answer_meta": None}) is None
    assert st.meta_view({**REC10, "status": "running", "answer": None}) == {"check": "unverified",
                                                                           "problem": "while_running"}
    legacy = {k: v for k, v in REC10.items() if k != "answer_meta"}
    assert st.meta_view(legacy) == {"check": "legacy_unknown"}
    assert st.meta_view({**legacy, "answer_meta": None}) == {"check": "legacy_unknown"}
    assert st.meta_view(raw({**F10, "extra": 1})) == {"check": "unverified", "problem": "shape"}
    assert st.meta_view(REC10) == {
        "check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
        "summary": {"state": "removed", "removals": [{"stage": "first_check", "cause": "K6"}],
                    "rewrite": "not_attempted"}}


# A-04b. check_wire accepts every meta_view output and rejects the wire-level cases with the same codes.
@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_a04b_every_meta_view_output_passes_check_wire(name):
    rec = FIXTURES[name]
    wired = {**rec, "answer_meta": st.meta_view(rec)}
    assert wired["answer_meta"]["check"] == "verified"
    assert st.check_wire(wired) is None


def wire_of(rec, **edits):
    wire = st.meta_view(rec)
    out = json.loads(json.dumps(wire))
    for path, value in edits.items():
        node = out
        keys = path.split("__")
        for key in keys[:-1]:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[int(keys[-1]) if isinstance(node, list) else keys[-1]] = value
    return {**rec, "answer_meta": out}


WIRE_N = [
    (lambda: wire_of(REC10, extra=1), "shape"),
    (lambda: {**REC10, "answer_meta": {"check": "unverified", "problem": "digest"}}, "shape"),
    (lambda: {**REC10, "answer_meta": {"check": "legacy_unknown"}}, "shape"),
    (lambda: {**REC10, "answer_meta": F10}, "shape"),
    (lambda: wire_of(REC10, summary__state="weird"), "enum"),
    (lambda: wire_of(REC10, summary__removals__0__cause="claim_cut"), "reasons"),
    (lambda: wire_of(REC10, summary__removals=[]), "reasons"),
    (lambda: wire_of(REC10, summary__state="shown"), "pairing"),
    (lambda: wire_of(REC10, execution__state="stopped_on_request"), "pairing"),
    (lambda: {**wire_of(REC10), "status": "failed"}, "pairing"),
    (lambda: wire_of(FIXTURES["F06"], execution__stop_reason=None), "pairing"),
    (lambda: wire_of(REC01, summary__rewrite="kept"), "reasons"),
    (lambda: {**wire_of(REC10), "answer": answer("partial", "now not blank", 4)}, "pairing"),
]


@pytest.mark.parametrize("build, code", WIRE_N)
def test_a04b_check_wire_rejects_the_wire_level_cases_with_the_same_code(build, code):
    assert st.check_wire(build()) == code


# A-04c. No clause is untested: disabling any one changes the outcome of at least one N case.
def test_a04c_every_clause_is_noticed_by_at_least_one_n_case(monkeypatch):
    baseline = run_n()
    assert list(baseline.values()) == [code for _, _, code in N]
    assert len(st.CLAUSES) >= 20
    for name in list(st.CLAUSES):
        code, _ = st.CLAUSES[name]
        with monkeypatch.context() as patch:
            patch.setitem(st.CLAUSES, name, (code, lambda facts: True))
            assert run_n() != baseline, f"clause {name} is not noticed by any N case"


# A-12. The module imports the standard library only and reads no file at import.
def test_a12_answer_state_imports_only_the_standard_library_and_reads_nothing_at_import(monkeypatch):
    source = Path(st.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported
    assert not [name for name in imported if name in ("core", "jsonschema", "yaml")]
    import builtins
    import importlib

    def no_open(*args, **kwargs):
        raise AssertionError("answer_state opened a file at import")

    monkeypatch.setattr(builtins, "open", no_open)
    saved = sys.modules.pop("core.agent.answer_state")
    try:
        importlib.import_module("core.agent.answer_state")
    finally:
        sys.modules["core.agent.answer_state"] = saved


# A-13. The digest binds blankness, status and claim count, not text.
def test_a13_gaps_and_dates_are_outside_the_digest_and_blankness_is_inside_it():
    rec = REC10
    edited = {**rec, "answer": {**rec["answer"], "gaps": [{"what": "other", "searched": "else", "why": "text"}],
                                "as_of": "2020-01-01"}}
    assert st.verify_stored(edited) is None
    assert st.verify_stored({**rec, "answer": {**rec["answer"], "short_answer": "now written"}}) == "bound"
    shown = {**REC01, "answer": {**REC01["answer"], "short_answer": ""}}
    assert st.verify_stored(shown) == "bound"


# 2.6 and the reader sentences.
def test_the_reader_sentences_are_the_contracted_copy_and_name_no_check_code():
    sentences = st.READER_SENTENCES
    assert sentences["removal:first_check|recheck:K6"] == (
        "The one-line summary was removed because it used a term or source the trust rules do not allow.")
    assert sentences["blank_unexplained"] == "No one-line summary was written for this answer."
    assert sentences["no_answer"] == "There is no answer: the run did not finish."
    assert sentences["legacy_or_unverified"] == "The one-line summary is not available for this answer."
    assert sentences["rewrite:removed_after_check"] == "A rewritten summary did not pass the checks either."
    assert sentences["shown_rewritten"] == "This summary was rewritten once from the findings that passed."
    for text in sentences.values():
        assert not re.search(r"\bK(10|[1-9])\b|critic\)", text)


# The wire value carries a version too.
def test_check_wire_reports_an_unknown_version():
    assert st.check_wire(wire_of(REC10, v=2)) == "version"


# The bound facts read blankness the way a reader does: whitespace is blank.
def test_bound_counts_a_whitespace_summary_as_blank():
    assert st.bound_facts({"status": "partial", "short_answer": " \t\n", "claims": []}) == {
        "answer_status": "partial", "summary_blank": True, "claims": 0}
    assert st.bound_facts({"status": "complete", "short_answer": "x", "claims": [{}]})["summary_blank"] is False
    assert st.bound_facts(None) == {"answer_status": None, "summary_blank": None, "claims": None}


# The ledger, stage by stage. The rows are the stage's own verdict rows; the expectations are literals.
def snap(text="A summary.", claims=None):
    return {"blank": not text.strip(), "claims": claims or {"c1": "one", "c2": "two"}}


def after(text="", claims=None):
    return {"short_answer": text, "claims": [{"id": k, "text": v} for k, v in (claims or {"c1": "one", "c2": "two"}).items()]}


def cut(rule, claim_id="short_answer"):
    return {"claim_id": claim_id, "rule": rule, "verdict": "cut"}


def observed(stage, before, after_answer, rows=()):
    ledger = st.SummaryLedger()
    ledger.observe(stage, before, after_answer, rows)
    return ledger


@pytest.mark.parametrize("stage", ["first_check", "recheck"])
def test_ledger_a_code_check_removal_takes_its_causes_from_the_rows_in_enum_order(stage):
    ledger = observed(stage, snap(), after(), [cut("K9"), cut("K2"), cut("K8", "c1")])
    assert ledger.removals == [(stage, "K2"), (stage, "K9")]  # the K8 row is a claim's, not the summary's


@pytest.mark.parametrize("stage", ["first_check", "recheck", "field_check", "support_check", "critic"])
def test_ledger_a_removal_no_evidence_names_is_unattributed(stage):
    assert observed(stage, snap(), after(claims={"c1": "one", "c2": "two"}), []).removals == [(stage, "unattributed")]


def test_ledger_the_field_check_reads_its_rules_and_the_unchecked_row():
    assert observed("field_check", snap(), after(), [cut("K6")]).removals == [("field_check", "K6")]
    assert observed("field_check", snap(), after(), [cut("field_check")]).removals == [("field_check", "field_unchecked")]
    assert observed("field_check", snap(), after(), [cut("K2")]).removals == [("field_check", "unattributed")]


def test_ledger_the_support_stage_separates_a_cut_claim_from_a_narrowed_one_and_lists_both_in_order():
    assert observed("support_check", snap(), after(claims={"c1": "one"})).removals == [("support_check", "claim_cut")]
    assert observed("support_check", snap(), after(claims={"c1": "one", "c2": "narrower"})).removals == [
        ("support_check", "claim_narrowed")]
    both = observed("support_check", snap(claims={"c1": "one", "c2": "two", "c3": "three"}),
                    after(claims={"c1": "one 2", "c3": "three"}))
    assert both.removals == [("support_check", "claim_cut"), ("support_check", "claim_narrowed")]


def test_ledger_the_critic_stage_records_only_a_cut_claim_even_if_a_kept_claim_changed():
    ledger = observed("critic", snap(), after(claims={"c1": "one changed"}))
    assert ledger.removals == [("critic", "claim_cut")]


def test_ledger_nothing_is_recorded_when_the_summary_was_blank_before_or_is_not_blank_after():
    assert observed("first_check", snap(""), after(""), [cut("K6")]).removals == []
    assert observed("first_check", snap(), after("still here"), [cut("K6")]).removals == []


def test_ledger_a_kept_rewrite_that_a_later_stage_removes_becomes_removed_after_check_but_not_an_earlier_one():
    later = st.SummaryLedger()
    later.note_rewrite("kept")
    later.observe("recheck", snap(), after(), [cut("K6")])
    assert later.rewrite == "removed_after_check"
    early = st.SummaryLedger()
    early.note_rewrite("kept")
    early.observe("support_check", snap(), after(claims={"c1": "one"}))
    assert early.rewrite == "kept"
    with pytest.raises(ValueError):
        early.note_rewrite("maybe")


def test_ledger_finalize_resets_for_fixed_text_and_refuses_a_contradiction():
    answer_in = {"status": "insufficient_evidence", "short_answer": "Not enough.", "claims": []}
    kept = st.SummaryLedger()
    kept.note_rewrite("kept")
    kept.removals.append(("support_check", "claim_cut"))
    assert kept.finalize(answer_in, "completed") == ("fixed_text", [], "removed_after_check")
    failed = st.SummaryLedger()
    failed.note_rewrite("no_budget")
    assert failed.finalize(answer_in, "completed") == ("fixed_text", [], "not_attempted")
    assert kept.finalize(answer_in, "stopped_on_request") == ("fixed_text", [], "not_attempted")
    contradiction = st.SummaryLedger()
    contradiction.removals.append(("first_check", "K6"))
    with pytest.raises(ValueError):
        contradiction.finalize({"status": "partial", "short_answer": "text", "claims": []}, "completed")
    assert st.SummaryLedger().finalize({"status": "complete", "short_answer": "", "claims": []}, "completed") == (
        "blank_unexplained", [], "not_attempted")
