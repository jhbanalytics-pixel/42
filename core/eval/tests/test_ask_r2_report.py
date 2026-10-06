import copy
import hashlib
import json
from datetime import date, datetime

import pytest

from core.eval.ask_r2_report import (
    QUESTION_IDS,
    load_morning_records,
    render_report,
)
from core.eval import ask_r2_report


def _morning_record(question_id):
    return {
        "id": question_id,
        "question": f"Question text for {question_id}",
        "run_id": f"morning-{question_id}",
        "outcome": "PASS" if question_id == "RISE-02" else "VALID INSUFFICIENT",
        "raw_answer": {"status": "partial", "short_answer": "morning answer", "evidence": [{"id": "m1"}]},
        "run": {
            "run_id": f"morning-{question_id}",
            "window": {"from": "2026-09-10", "to": "2026-09-30"},
            "posts": 12,
        },
        "bars": {
            "pass": question_id == "RISE-02",
            "schema_valid": True,
            "cited": 6 if question_id == "RISE-02" else 2,
            "cited_in_window": 6 if question_id == "RISE-02" else 2,
            "platforms": ["tiktok", "x"] if question_id == "RISE-02" else ["x"],
            "unknown_ids": 0,
            "age_claims": 0,
            "numbers": 1,
            "numbers_reproduced": 1,
        },
        "reported_model_usd": 0.125,
        "booked_usd": 0.125,
        "query_hashes": {"q1": "b" * 64},
        "citations": [{"id": "m1", "platform": "x", "url": "https://example.test/m1"}],
        "claim_rows": [{"checker": "code", "rule": "K3", "verdict": "pass", "reason": None}],
    }


def _morning_records():
    return {question_id: _morning_record(question_id) for question_id in QUESTION_IDS}


def _metadata(**extra):
    return {
        "cap_usd": 10,
        "charged_usd": 1.23456789,
        "cost_scope": "The USD cap covers Gemini request tokens. Semantic TVF search is refused; warehouse bytes are separate.",
        "not_run_reasons": {
            "WHY-03": "operator_gate_closed",
            "SPR-03": "operator_gate_closed",
            "CRE-01": "operator_gate_closed",
        },
        **extra,
    }


def test_loader_checks_supplied_capture_hashes_and_report_preserves_inputs(tmp_path, monkeypatch):
    morning = _morning_records()
    model_costs = [1.999443225, 1.3917287625000005, 2.0515045125000007, 1.3652800874999997, 0.6050021250000001]
    booked_costs = [1.999443, 1.391729, 2.051505, 1.365280, 0.605002]
    payloads = {}
    for question_id, model_cost, booked_cost in zip(QUESTION_IDS, model_costs, booked_costs, strict=True):
        morning[question_id]["reported_model_usd"] = model_cost
        morning[question_id]["booked_usd"] = booked_cost
        payloads[question_id] = json.dumps(morning[question_id], ensure_ascii=False, indent=2).encode("utf-8")
        (tmp_path / f"{question_id}.json").write_bytes(payloads[question_id])
    expected_hashes = {question_id: hashlib.sha256(payload).hexdigest() for question_id, payload in payloads.items()}
    monkeypatch.setattr(ask_r2_report, "FROZEN_MORNING_SHA256", expected_hashes)
    before = {
        question_id: hashlib.sha256((tmp_path / f"{question_id}.json").read_bytes()).hexdigest()
        for question_id in QUESTION_IDS
    }
    morning, provenance = load_morning_records(tmp_path, expected_hashes=expected_hashes)
    snapshot = copy.deepcopy(morning)
    metadata = _metadata(
        charged_usd=0,
        not_run_reasons={question_id: "operator_gate_closed" for question_id in QUESTION_IDS},
        morning_source_paths=provenance["source_paths"],
        morning_sha256=provenance["sha256"],
    )
    report = render_report([], list(QUESTION_IDS), metadata, morning)
    after = {
        question_id: hashlib.sha256((tmp_path / f"{question_id}.json").read_bytes()).hexdigest()
        for question_id in QUESTION_IDS
    }

    assert before == after == expected_hashes
    assert provenance["sha256"] == expected_hashes
    assert set(provenance["source_paths"]) == set(QUESTION_IDS)
    assert morning == snapshot
    assert "| RISE-02 | R2 verdict: refused<br>NOT RUN" in report
    assert "Morning baseline" in report
    assert "Below the five post bar" in report
    assert "R2 reported runner amount total: 0 USD." in report
    assert "Morning tracked token priced total: 7.4129587125 USD." in report
    assert "Morning rounded booked total: 7.412959 USD." in report
    assert "Health at start:" in report
    assert "R2 session booked readback: unverified" in report
    assert "Vendor billing reconciliation: unproven." in report


def test_report_keeps_insufficient_error_and_not_run_states_and_usd_units():
    morning = _morning_records()
    results = [
        {
            "id": "RISE-02",
            "question": "Question text for RISE-02",
            "run_id": "r-rise",
            "prefix": "ask-r2-2026-09-30-RISE-02",
            "outcome": "VALID INSUFFICIENT",
            "raw_answer": {
                "status": "insufficient_evidence",
                "short_answer": "No supported answer",
                "evidence": [{"id": "r1", "platform": "x", "url": "https://example.test/r1"}],
                "access_token": "do-not-print-this-secret",
            },
            "run": {"run_id": "r-rise", "window": {"from": "2026-09-12", "to": "2026-09-30"}, "posts": 17},
            "query_hashes": {"q1": "a" * 64},
            "claim_rows": [{
                "checker": "model", "rule": "K4", "verdict": "cut",
                "reason": "unsupported claim from C:\\Users\\AlbertMeintjes\\OneDrive - Ogilvy\\private exception\\exception.py:99",
            }],
            "claim_readback": {"match": True, "rows": []},
            "spend_writes": [{
                "run_id": "ask-r2-2026-09-30-RISE-02-spend-550e8400",
                "stage": "understand_spend", "run_date": "2026-09-30", "status": "ok",
                "model_usd": 1.234568, "what": "staging_check_ask:RISE-02",
            }],
            "spend_intents": [{"run_id": "r-rise", "booked_usd": 1.234568}],
            "spend_readback": {"match": True, "rows": [{
                "run_id": "ask-r2-2026-09-30-RISE-02-spend-550e8400",
                "stage": "understand_spend", "status": "ok", "what": "staging_check_ask:RISE-02",
                "booked_usd": 1.234568,
            }]},
            "call_costs": [{
                "phase": "answer", "model": "configured-model", "status": "charged",
                "reserved_usd": 1.5, "charged_usd": 1.23456789,
            }],
            "charged_usd": 1.23456789,
            "bars": {
                "pass": False, "cited": 1, "cited_in_window": 1, "platforms": ["x"],
                "unknown_ids": 0, "age_claims": 0, "numbers": 1, "numbers_reproduced": 0,
            },
            "warehouse_billed_bytes": 4096,
            "context": {"events": [{"step": "r2_coverage_gap", "reason": "semantic_tvf_refused"}]},
        },
        {
            "id": "NOW-01", "run_id": "r-now", "prefix": "ask-r2-2026-09-30-NOW-01",
            "outcome": "ERROR", "error_type": "TimeoutError",
            "stop_reason": "warehouse_unavailable", "raw_answer": None,
            "run": {"run_id": "r-now"}, "charged_usd": 0,
            "spend_intents": [], "spend_writes": [], "spend_readback": {"match": True, "rows": []},
        },
    ]
    metadata = _metadata(session_booked_usd=1.234568, session_booked_verified=True)
    before = copy.deepcopy((results, morning, metadata))

    report = render_report(results, ["WHY-03", "SPR-03", "CRE-01"], metadata, morning)

    assert (results, morning, metadata) == before
    assert "VALID INSUFFICIENT" in report
    assert "Answer status: insufficient_evidence" in report
    assert "R2 verdict: refused<br>NOT RUN<br>Cause: operator_gate_closed" in report
    assert "R2 verdict: refused" in report
    assert "No supported answer" in report
    assert "TimeoutError, warehouse_unavailable" in report
    assert "Retrieval coverage differs" in report
    assert "R2 RISE-02 coverage gap: semantic_tvf_refused." in report
    assert "R2 warehouse billed bytes: unverified" in report
    assert "2026-09-12 to 2026-09-30; captured posts: 17" in report
    assert "R2 reported runner amount total: 1.23456789 USD." in report
    assert "Rounded booked total from matched spend readbacks: 1.234568 USD." in report
    assert "R2 session booked readback: 1.234568 USD." in report
    assert "Session cap: 10 USD." in report
    assert "Vendor billing reconciliation: unproven." in report
    assert "do-not-print-this-secret" not in report
    assert "exception.py" not in report
    assert "OneDrive - Ogilvy" not in report


def test_source_health_provenance_shows_distinct_versions_without_a_common_deployment_claim():
    morning = _morning_records()
    stopped = list(QUESTION_IDS)
    causes = {question_id: "operator_gate_closed" for question_id in QUESTION_IDS}
    source_sha = "a" * 40
    deployed_sha = "e" * 40
    metadata = _metadata(
        charged_usd=0,
        not_run_reasons=causes,
        ready=False,
        gate_validated=False,
        source_commit=source_sha,
        source_clean=True,
        deployed_sha=deployed_sha,
    )

    blocked = render_report([], stopped, metadata, morning)
    assert "Health at start:" in blocked
    assert "Original source details from resume provenance:" in blocked
    assert source_sha not in blocked
    assert deployed_sha not in blocked

    metadata.update({
        "ready": True,
        "gate_validated": True,
        "health_at_start": {"version": "health-46"},
        "resume_provenance": {
            "original_source_details": {"source": source_sha, "deployed": deployed_sha},
            "original_source_commit": source_sha,
        },
    })
    ready = render_report([], stopped, metadata, morning)

    assert "health-46" in ready
    assert source_sha in ready and deployed_sha in ready
    assert "Original source details from resume provenance" in ready
    assert "Common deployed SHA" not in ready


def test_mismatched_spend_row_identity_or_reason_is_not_booked():
    morning = _morning_records()
    prefix = "ask-r2-2026-09-30-RISE-02"
    record = {
        "id": "RISE-02", "prefix": prefix, "run_id": "r-ask-run", "outcome": "PASS",
        "raw_answer": {"status": "partial"}, "charged_usd": 0.1,
        "spend_writes": [{
            "run_id": prefix + "-spend-abc", "stage": "understand_spend", "status": "ok",
            "what": "staging_check_ask:RISE-02", "model_usd": 0.1,
        }],
        "spend_readback": {"match": True, "rows": [{
            "run_id": prefix + "-spend-abc", "stage": "understand_spend", "status": "ok",
            "what": "staging_check_ask:CRE-01", "booked_usd": 0.1,
        }]},
    }
    metadata = _metadata(
        charged_usd=0.1,
        not_run_reasons={
            "NOW-01": "operator_gate_closed",
            "WHY-03": "operator_gate_closed",
            "SPR-03": "operator_gate_closed",
            "CRE-01": "operator_gate_closed",
        },
    )

    report = render_report([record], ["NOW-01", "WHY-03", "SPR-03", "CRE-01"], metadata, morning)

    assert "Rounded booked USD: unverified" in report
    assert "Rounded booked total: unverified" in report


def test_r2_verdict_requires_complete_answer_and_both_local_readbacks():
    morning = _morning_records()

    def result(question_id, status, outcome, *, accepted, readback_match):
        run_id = f"r-{question_id}"
        return {
            "id": question_id,
            "question": f"Question text for {question_id}",
            "prefix": f"ask-r2-{question_id}",
            "run_id": run_id,
            "outcome": outcome,
            "raw_answer": {"status": status, "short_answer": f"answer for {question_id}"},
            "run": {"run_id": run_id},
            "charged_usd": 0,
            "bars": {"schema_valid": accepted, "pass": accepted},
            "claim_readback": {"match": readback_match, "rows": []},
            "spend_writes": [],
            "spend_intents": [],
            "spend_readback": {"match": readback_match, "rows": []},
        }

    records = [
        result("NOW-01", "complete", "PASS", accepted=True, readback_match=True),
        result("RISE-02", "partial", "PARTIAL", accepted=False, readback_match=False),
        result("WHY-03", "partial", "OPERATIONAL STOP", accepted=False, readback_match=False),
        result("SPR-03", "complete", "PASS", accepted=True, readback_match=False),
    ]
    metadata = _metadata(
        charged_usd=0,
        not_run_reasons={"CRE-01": "execution_authority_required"},
    )

    report = render_report(records, ["CRE-01"], metadata, morning)

    assert "| NOW-01 | R2 verdict: full answer with local receipts<br>" in report
    assert "| RISE-02 | R2 verdict: partial<br>" in report
    assert "| WHY-03 | R2 verdict: refused<br>" in report
    assert "| SPR-03 | R2 verdict: partial<br>" in report
    assert "| CRE-01 | R2 verdict: refused<br>NOT RUN<br>Cause: execution_authority_required" in report
    assert "R2 verdict: full answer with local receipts" in report


def test_r3_report_labels_comparison_and_separates_known_charges_from_reserved_ceiling():
    morning = _morning_records()
    prefix = "l3-ask-20260930-r3-NOW-01"
    record = _retry_attempt(
        "NOW-01", "NOW-01", prefix, "run-r3-now", "PASS", "complete", 0.54,
        call_costs=[
            {"phase": "answer", "status": "charged_known", "reserved_usd": 0.04, "charged_usd": 0.04},
            {"phase": "research", "status": "unknown_charged_ceiling", "reserved_usd": 0.3, "charged_usd": 0.3},
            {"phase": "embedding", "status": "charged_conservative_ceiling",
             "charge_basis": "conservative_embedding_ceiling", "reserved_usd": 0.12, "charged_usd": 0.12},
            {"phase": "embedding", "status": "charged_conservative_ceiling",
             "charge_basis": "bounded_usage_missing", "reserved_usd": 0.08, "charged_usd": 0.08},
        ],
    )
    record["context"]["events"] = [
        {"step": "retrieval_trace", "mode": "keyword", "query_id": "q-keyword",
         "row_count": 2, "post_ids": ["p1", "p2"], "status": "success", "refusal_category": None},
        {"step": "retrieval_trace", "mode": "semantic", "query_id": "q-semantic",
         "row_count": 0, "post_ids": [], "status": "refused", "refusal_category": "guard"},
    ]
    metadata = _metadata(
        evaluation_label="R3",
        charged_usd=0.54,
        source_commit="a" * 40,
        embedding_coverage={
            "status": "ok",
            "as_of_date": "2026-09-30",
            "window_start_date": "2026-09-24",
            "window_end_date": "2026-09-30",
            "window_days": 7,
            "window_basis": "inclusive SAST calendar dates",
            "current_day_partial": True,
            "latest_embedding_run": {"run_id": "embed-1", "run_date": "2026-09-30", "status": "ok"},
            "markets": {
                "ZA": {"embedded_distinct_posts": 3, "eligible_distinct_posts": 5},
                "NG": {"embedded_distinct_posts": None, "eligible_distinct_posts": 2},
            },
        },
        not_run_reasons={
            question_id: "operator_gate_closed" for question_id in QUESTION_IDS if question_id != "NOW-01"
        },
    )

    report = render_report(
        [record], [question_id for question_id in QUESTION_IDS if question_id != "NOW-01"], metadata, morning
    )

    assert "# Ask R3 comparison" in report
    assert "| Question | Latest R3 result | Prior R3 attempts | Morning baseline |" in report
    assert "R3 verdict: full answer with local receipts" in report
    comparison = report.split("| Question | Latest R3 result | Prior R3 attempts | Morning baseline |", 1)[1]
    comparison = comparison.split("## Spend accounting", 1)[0]
    assert comparison.count("R3 verdict:") == 5
    assert "R2 verdict:" not in report
    assert "R3 reported runner amount total: 0.54 USD." in report
    assert "Known-dispatch call charge subtotal: 0.04 USD." in report
    assert "Unknown-dispatch ceiling: 0.3 USD; not measured token usage." in report
    assert "Conservative reservation subtotal: 0.20 USD; unmeasured reserved amount." in report
    assert "R3 spend prefix: l3-ask-20260930-r3-NOW-01" in report
    assert "R3 raw answer" in report
    assert "R3 clean source commit at start:" in report
    assert "a" * 40 in report
    assert "Latest embedding run date: 2026-09-30." in report
    assert "| ZA | 2026-09-30 | 3 | 5 |" in report
    assert "| NG | 2026-09-30 | unverified | 2 |" in report
    assert "q-keyword" in report and "q-semantic" in report
    assert "| NOW-01 | semantic | refused | guard | q-semantic | 0 | [] | unverified |" in report
    assert "NOW-01 raw retrieval_trace events:" in report


def test_r3_report_rejects_replayed_question_attempt_key():
    morning = _morning_records()
    record = _retry_attempt(
        "NOW-01", "NOW-01-retry-1", "l3-ask-20260930-r3-NOW-01-retry-1",
        "run-r3-now", "PASS", "complete", 0.1,
    )
    metadata = _metadata(
        evaluation_label="R3",
        not_run_reasons={question_id: "operator_gate_closed" for question_id in QUESTION_IDS if question_id != "NOW-01"},
    )

    with pytest.raises(ValueError, match="R3 NOW-01 retry requires its original attempt"):
        render_report([record], [question_id for question_id in QUESTION_IDS if question_id != "NOW-01"], metadata, morning)


@pytest.mark.parametrize("resume_metadata", [
    {"funding": {"baseline_spend_usd": 1, "additional_cap_usd": 8, "combined_total_cap_usd": 9}},
    {"resume_provenance": {"original_source_commit": "77c8fcdf"}},
    {"not_run_attempts": [{"id": "RISE-02", "attempt_key": "RISE-02-retry-1"}]},
])
def test_r3_report_rejects_r2_resume_funding_or_pending_retry_metadata(resume_metadata):
    metadata = _metadata(
        evaluation_label="R3",
        not_run_reasons={question_id: "operator_gate_closed" for question_id in QUESTION_IDS},
        **resume_metadata,
    )

    with pytest.raises(ValueError, match="R3 does not allow"):
        render_report([], list(QUESTION_IDS), metadata, _morning_records())


def test_report_serializes_date_and_datetime_values_in_local_receipts():
    morning = _morning_records()
    question_id = "NOW-01"
    prefix = "ask-r2-2026-09-30-NOW-01"
    spend_id = prefix + "-spend-550e8400"
    run_date = date(2026, 9, 30)
    checked_at = datetime(2026, 9, 30, 10, 30)
    write = {
        "run_id": spend_id,
        "stage": "understand_spend",
        "run_date": run_date,
        "status": "ok",
        "model_usd": 0.1,
        "what": "staging_check_ask:NOW-01",
    }
    record = {
        "id": question_id,
        "question": "Question text for NOW-01",
        "prefix": prefix,
        "run_id": "r-now",
        "outcome": "PASS",
        "raw_answer": {"status": "complete", "short_answer": "complete answer"},
        "run": {"run_id": "r-now", "window": {"from": "2026-09-24", "to": "2026-09-30"}, "posts": 17},
        "charged_usd": 0.1,
        "bars": {"schema_valid": True, "pass": True},
        "claim_readback": {"match": True, "rows": [{"checked_at": checked_at}]},
        "spend_writes": [write],
        "spend_intents": [{"run_date": run_date}],
        "spend_readback": {
            "match": True,
            "rows": [{
                "run_id": spend_id,
                "stage": "understand_spend",
                "run_date": run_date,
                "status": "ok",
                "what": "staging_check_ask:NOW-01",
                "booked_usd": 0.1,
            }],
        },
    }
    metadata = _metadata(
        charged_usd=0.1,
        not_run_reasons={
            question_id: "operator_gate_closed"
            for question_id in QUESTION_IDS
            if question_id != "NOW-01"
        },
    )

    report = render_report([record], [question_id for question_id in QUESTION_IDS if question_id != "NOW-01"], metadata, morning)

    assert "2026-09-30" in report
    assert "2026-09-30T10:30:00" in report
    assert "R2 verdict: full answer with local receipts" in report


def test_loader_rejects_hash_mismatch_and_record_id_mismatch(tmp_path):
    for question_id in QUESTION_IDS:
        (tmp_path / f"{question_id}.json").write_text(json.dumps({"id": question_id}), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch for NOW-01"):
        load_morning_records(tmp_path)

    (tmp_path / "NOW-01.json").write_text(json.dumps({"id": "wrong"}), encoding="utf-8")
    with pytest.raises(ValueError, match="ID mismatch for NOW-01"):
        load_morning_records(tmp_path, expected_hashes=None)


def test_report_rejects_unaccounted_slots_or_missing_stop_cause():
    morning = _morning_records()
    with pytest.raises(ValueError, match="account for each question"):
        render_report([], list(QUESTION_IDS[:-1]), _metadata(), morning)

    metadata = _metadata(not_run_reasons={"WHY-03": "operator_gate_closed", "SPR-03": "operator_gate_closed"})
    with pytest.raises(ValueError, match="safe stop cause code missing for CRE-01"):
        render_report([{"id": "NOW-01"}, {"id": "RISE-02"}], ["WHY-03", "SPR-03", "CRE-01"], metadata, morning)


def _retry_attempt(question_id, attempt_key, prefix, run_id, outcome, status, cost, *, call_costs=None):
    success = status == "complete" and outcome == "PASS"
    spend_id = f"{prefix}-spend-{attempt_key}"
    return {
        "id": question_id,
        "attempt_key": attempt_key,
        "prefix": prefix,
        "run_id": run_id,
        "outcome": outcome,
        "raw_answer": {"status": status, "short_answer": f"answer for {attempt_key}"},
        "run": {"run_id": run_id, "window": {"from": "2026-09-24", "to": "2026-09-30"}, "posts": 11},
        "charged_usd": cost,
        "accounting_status": "unknown_dispatched_cost" if status == "refused" else "complete",
        "bars": {"pass": success, "schema_valid": success, "cited": 6 if success else 0,
                 "cited_in_window": 6 if success else 0, "platforms": ["x"] if success else []},
        "claim_readback": {"match": success, "rows": []},
        "spend_writes": [{
            "run_id": spend_id, "stage": "understand_spend", "status": "ok",
            "what": f"staging_check_ask:{question_id}", "model_usd": cost,
        }],
        "spend_intents": [{"run_id": run_id, "booked_usd": cost}],
        "spend_readback": {"match": True, "rows": [{
            "run_id": spend_id, "stage": "understand_spend", "status": "ok",
            "what": f"staging_check_ask:{question_id}", "booked_usd": cost,
        }]},
        "call_costs": call_costs or [{
            "phase": "answer", "model": "configured-model", "status": "charged_known",
            "reserved_usd": cost, "charged_usd": cost,
        }],
        "context": {"queries": {"q1": {
            "sql": "SELECT 1", "params": [], "rows": [], "result_hash": "a" * 64, "purpose": "research",
        }}},
        "warehouse_billed_bytes": 0,
    }


def _retry_scenario():
    base_prefix = "l3-ask-20260930-r2-"
    original_rise_costs = [
        {"phase": "answer", "model": "gemini-3.8-flash", "status": "charged_known",
         "reserved_usd": 0.006923, "charged_usd": 0.006923},
        {"phase": "answer", "model": "gemini-3.8-flash", "status": "charged_known",
         "reserved_usd": 0.005121, "charged_usd": 0.005121},
        {"phase": "research", "model": "gemini-3.8-flash", "status": "unknown_charged_ceiling",
         "reserved_usd": 0.607707, "charged_usd": 0.607707, "input_bound_tokens": 355138,
         "output_reserve_tokens": 10000, "actual_input_tokens": 355138, "actual_output_tokens": 10000},
        {"phase": "research", "model": "gemini-3.8-flash", "status": "refused_before_dispatch",
         "reserved_usd": 0, "charged_usd": 0},
    ]
    results = [
        _retry_attempt("NOW-01", "NOW-01", base_prefix + "NOW-01", "run-now", "PARTIAL", "partial", 1.375862),
        _retry_attempt("RISE-02", "RISE-02", base_prefix + "RISE-02", "run-rise-original",
                       "ERROR", "refused", 0.619751, call_costs=original_rise_costs),
        _retry_attempt("WHY-03", "WHY-03", base_prefix + "WHY-03", "run-why", "PASS", "complete", 0.7),
        _retry_attempt("SPR-03", "SPR-03", base_prefix + "SPR-03", "run-spr", "PASS", "complete", 0.8),
        _retry_attempt("CRE-01", "CRE-01", base_prefix + "CRE-01", "run-cre", "PASS", "complete", 0.9),
        _retry_attempt("RISE-02", "RISE-02-retry-1", base_prefix + "RISE-02-retry-1", "run-rise-retry",
                       "PASS", "complete", 0.5),
    ]
    results[1]["error_type"] = "ServerError"
    costs = [1.375862, 0.619751, 0.7, 0.8, 0.9, 0.5]
    metadata = _metadata(
        cap_usd=10,
        charged_usd=sum(costs),
        not_run_reasons={},
        funding={"baseline_spend_usd": 1.995613, "additional_cap_usd": 8,
                 "combined_total_cap_usd": 9.995613},
        attempts=[{
            "id": record["id"], "attempt_key": record["attempt_key"], "outcome": record["outcome"],
            "charged_usd": record["charged_usd"], "prefix": record["prefix"],
            "stop_reason": record.get("stop_reason"), "session_cap_usd": 10,
        } for record in results],
        health_at_start={"source_sha": "77", "deployed_sha": "46", "health_version": "46"},
        resume_provenance={
            "original_source_details": {"source": "77c8fcdf", "deployed": "46"},
            "original_source_commit": "77c8fcdf",
        },
    )
    results[1]["source_provenance"] = {"source_stage": "f4", "health_at_resume": "health-38"}
    results[-1]["source_provenance"] = {"source_stage": "retry-source", "health_at_resume": "health-retry"}
    metadata["not_run_reasons"] = {question_id: "operator_gate_closed" for question_id in QUESTION_IDS if question_id not in {record["id"] for record in results}}
    metadata["session_booked_usd"] = sum(costs)
    metadata["session_booked_verified"] = True
    return results, metadata


def _r3_continuation_scenario():
    continuation = {
        "source_metadata_sha256": "d70a5f142f98488163e278ac67b9172afce1d13cea0156f17de32cfb51522ab7",
        "original_source_commit": "15342e3c8faff2a1516443e55ca955403af37f0d",
        "original_report_sha256": "87e407759caa165f66b11dd1289a7174b279ba319c017e421ad2ddc6d9a97639",
        "original_now_record_sha256": "c3d4c2d14a089a369e08178a301f93fdb0e8ce73fdf1cd466d5104b742e2d974",
        "initial_booked_usd": "1.357150",
        "resume_source_commit": "15342e3c8faff2a1516443e55ca955403af37f0d",
        "health_at_resume": {"status": "ok"},
    }
    original = _retry_attempt(
        "NOW-01", "NOW-01", "l3-ask-20260930-r3-NOW-01", "run-r3-now-original",
        "ERROR", "refused", 1.357150,
        call_costs=[{
            "phase": "answer", "model": "configured-model", "status": "unknown_charged_ceiling",
            "reserved_usd": 1.357150, "charged_usd": 1.357150,
        }],
    )
    original["raw_answer"] = None
    original["error_type"] = "ServerError"
    original["http_status"] = 504
    original["stop_reason"] = "HTTP504"
    records = [original]
    records.extend([
        _retry_attempt("RISE-02", "RISE-02", "l3-ask-20260930-r3-RISE-02", "run-r3-rise", "PASS", "complete", 0.6),
        _retry_attempt("WHY-03", "WHY-03", "l3-ask-20260930-r3-WHY-03", "run-r3-why", "PASS", "complete", 0.7),
        _retry_attempt("SPR-03", "SPR-03", "l3-ask-20260930-r3-SPR-03", "run-r3-spr", "PASS", "complete", 0.8),
        _retry_attempt("CRE-01", "CRE-01", "l3-ask-20260930-r3-CRE-01", "run-r3-cre", "PASS", "complete", 0.9),
    ])
    retry = _retry_attempt(
        "NOW-01", "NOW-01-retry-1", "l3-ask-20260930-r3-NOW-01-retry-1", "run-r3-now-retry",
        "PASS", "complete", 0.75,
    )
    for resumed in records[1:] + [retry]:
        resumed["r3_continuation"] = copy.deepcopy(continuation)
    records.append(retry)
    total = sum(record["charged_usd"] for record in records)
    metadata = _metadata(
        evaluation_label="R3",
        cap_usd=10,
        session_cap_usd=10,
        charged_usd=total,
        source_commit=continuation["resume_source_commit"],
        not_run_reasons={},
        r3_continuation=continuation,
        attempts=[{
            "id": record["id"], "attempt_key": record["attempt_key"], "outcome": record["outcome"],
            "charged_usd": record["charged_usd"], "prefix": record["prefix"], "session_cap_usd": 10,
        } for record in records],
        session_booked_usd=total,
        session_booked_verified=True,
    )
    return records, metadata, continuation


def test_r3_continuation_groups_only_now_retry_and_counts_each_record_once():
    results, metadata, continuation = _r3_continuation_scenario()
    morning = _morning_records()
    before = copy.deepcopy((results, metadata, morning))

    report = render_report(results, [], metadata, morning)

    comparison = report.split("## Spend accounting", 1)[0]
    now_row = next(line for line in comparison.splitlines()
                   if line.startswith("| NOW-01 |") and "verdict:" in line)
    assert "R3 verdict: full answer with local receipts" in now_row
    assert "Attempt NOW-01: R3 verdict refused" in now_row
    assert sum(line.startswith("| NOW-01 |") and "R3 verdict:" in line
               for line in comparison.splitlines()) == 1
    assert "run-r3-now-original" in report and "run-r3-now-retry" in report
    assert "l3-ask-20260930-r3-NOW-01-retry-1" in report
    assert "R3 reported runner amount total: 5.10715 USD." in report
    assert "Per-attempt reported amount sum: 5.10715 USD; aggregate matches." in report
    assert "Already booked before R3 continuation: 1.357150 USD; reference only, not added separately from continuation metadata." in report
    assert "R3 continuation provenance" in report
    assert continuation["original_report_sha256"] in report
    assert continuation["source_metadata_sha256"] in report
    attempt_section = report.split("Attempt ledger", 1)[1].split("Morning totals", 1)[0]
    attempt_rows = [line for line in attempt_section.splitlines()
                    if line.startswith("| ") and not line.startswith("|---") and not line.startswith("| Question |")]
    assert len(attempt_rows) == 6
    assert "Combined retry ceiling" not in report
    assert (results, metadata, morning) == before


def test_r3_report_rejects_unauthorized_retry_key_and_duplicate_attempt_manifest():
    results, metadata, _ = _r3_continuation_scenario()
    unauthorized_attempts = [
        _retry_attempt("NOW-01", "NOW-01-retry-2", "l3-ask-20260930-r3-NOW-01-retry-2",
                       "run-r3-now-retry-2", "PASS", "complete", 0.2),
        _retry_attempt("RISE-02", "RISE-02-retry-1", "l3-ask-20260930-r3-RISE-02-retry-1",
                       "run-r3-rise-retry", "PASS", "complete", 0.2),
    ]
    for unauthorized in unauthorized_attempts:
        with pytest.raises(ValueError, match="R3 allows only NOW-01 retry-1"):
            render_report(results + [unauthorized], [], metadata, _morning_records())

    duplicate_manifest = copy.deepcopy(metadata)
    duplicate_manifest["attempts"].append(copy.deepcopy(duplicate_manifest["attempts"][0]))
    with pytest.raises(ValueError, match="duplicate attempt key"):
        render_report(results, [], duplicate_manifest, _morning_records())


def test_r3_report_allows_only_the_identified_pending_now_retry_overlap():
    results, metadata, _ = _r3_continuation_scenario()
    original = results[0]
    metadata["attempts"] = metadata["attempts"][:1]
    metadata["charged_usd"] = 1.357150
    metadata["session_booked_usd"] = 1.357150
    metadata["not_run_reasons"] = {question_id: "operator_gate_closed" for question_id in QUESTION_IDS}
    metadata["not_run_attempts"] = [{"id": "NOW-01", "attempt_key": "NOW-01-retry-1"}]

    report = render_report([original], list(QUESTION_IDS), metadata, _morning_records())

    now_row = next(line for line in report.split("## Spend accounting", 1)[0].splitlines()
                   if line.startswith("| NOW-01 |") and "verdict:" in line)
    assert "R3 verdict: refused" in now_row
    assert "Retry pending: NOW-01-retry-1" in now_row
    assert "R3 attempt NOW-01-retry-1: NOT RUN" in report


def test_report_groups_successful_rise_retry_and_accounts_for_all_six_attempts_without_mutation():
    results, metadata = _retry_scenario()
    morning = _morning_records()
    before = copy.deepcopy((results, morning, metadata))
    not_run = [question_id for question_id in QUESTION_IDS if question_id not in {record["id"] for record in results}]

    report = render_report(results, not_run, metadata, morning)

    comparison = report.split("## Spend accounting", 1)[0]
    rise_row = next(line for line in comparison.splitlines() if line.startswith("| RISE-02 |"))
    assert comparison.count("| RISE-02 |") == 1
    assert "R2 verdict: full answer with local receipts" in rise_row
    assert "Attempt RISE-02: R2 verdict refused" in rise_row
    assert "0.619751 USD" in rise_row
    assert "R2 verdict: refused" in report
    assert "ServerError" in report
    assert "run-rise-original" in report
    assert "l3-ask-20260930-r2-RISE-02" in report
    assert "l3-ask-20260930-r2-RISE-02-retry-1" in report
    assert "R2 reported runner amount total: 4.895613 USD" in report
    assert "Per-attempt reported amount sum: 4.895613 USD; aggregate matches." in report
    assert "Rounded booked total from matched spend readbacks: 4.895613 USD." in report
    assert "Known-dispatch call charge subtotal: 4.287906 USD." in report
    assert "Unknown-dispatch ceiling: 0.607707 USD; not measured token usage." in report
    assert "Fallback token bounds are not measured usage." in report
    assert "Session cap: 10 USD." in report
    assert "Combined retry ceiling: 9.995613 USD (baseline 1.995613 USD plus additional 8 USD)." in report
    assert "health-38" in report and "health-retry" in report
    assert "77c8fcdf" in report and "retry-source" in report
    assert "source_stage" in report
    attempt_section = report.split("Attempt ledger", 1)[1].split("Morning totals", 1)[0]
    attempt_rows = [line for line in attempt_section.splitlines()
                    if line.startswith("| ") and not line.startswith("|---") and not line.startswith("| Question |")]
    assert len(attempt_rows) == 6
    assert "R2 warehouse billed bytes: unverified" in report
    assert "0 bytes" not in report
    assert "raw answer" in report
    assert (results, morning, metadata) == before


def test_report_allows_only_the_identified_pending_rise_retry_overlap():
    results, metadata = _retry_scenario()
    results = results[:2]
    metadata["attempts"] = metadata["attempts"][:2]
    metadata["charged_usd"] = 1.995613
    metadata["session_booked_usd"] = 1.995613
    metadata["not_run_reasons"] = {
        "RISE-02": "retry_pending",
        "WHY-03": "operator_gate_closed",
        "SPR-03": "operator_gate_closed",
        "CRE-01": "operator_gate_closed",
    }
    metadata["not_run_attempts"] = [{"id": "RISE-02", "attempt_key": "RISE-02-retry-1"}]
    not_run = ["RISE-02", "WHY-03", "SPR-03", "CRE-01"]

    report = render_report(results, not_run, metadata, _morning_records())

    comparison = report.split("## Spend accounting", 1)[0]
    rise_row = next(line for line in comparison.splitlines() if line.startswith("| RISE-02 |"))
    assert "R2 verdict: refused" in rise_row
    assert "Retry pending: RISE-02-retry-1; cause: retry_pending" in rise_row
    assert "R2 attempt RISE-02-retry-1: NOT RUN, cause code" in report

    metadata.pop("not_run_attempts")
    with pytest.raises(ValueError, match="may overlap only for the identified pending RISE-02 retry"):
        render_report(results, not_run, metadata, _morning_records())


def test_report_rejects_duplicate_attempt_keys_and_run_ids():
    morning = _morning_records()
    results, metadata = _retry_scenario()
    not_run = []

    duplicate_key = copy.deepcopy(results)
    duplicate_key.append(copy.deepcopy(duplicate_key[-1]))
    duplicate_key[-1]["run_id"] = "run-rise-duplicate-key"
    duplicate_key[-1]["run"]["run_id"] = "run-rise-duplicate-key"
    with pytest.raises(ValueError, match="duplicate attempt key"):
        render_report(duplicate_key, not_run, metadata, morning)

    duplicate_run_id = copy.deepcopy(results)
    duplicate_run_id[-1]["run_id"] = duplicate_run_id[1]["run_id"]
    duplicate_run_id[-1]["run"]["run_id"] = duplicate_run_id[1]["run_id"]
    with pytest.raises(ValueError, match="duplicate R2 run ID"):
        render_report(duplicate_run_id, not_run, metadata, morning)

    extra_retry = copy.deepcopy(results)
    extra_retry_record = copy.deepcopy(extra_retry[-1])
    extra_retry_record["attempt_key"] = "RISE-02-retry-2"
    extra_retry_record["prefix"] = "l3-ask-20260930-r2-RISE-02-retry-2"
    extra_retry_record["run_id"] = "run-rise-retry-2"
    extra_retry_record["run"]["run_id"] = "run-rise-retry-2"
    retry_2_spend_id = extra_retry_record["prefix"] + "-spend-RISE-02-retry-2"
    extra_retry_record["spend_writes"][0]["run_id"] = retry_2_spend_id
    extra_retry_record["spend_readback"]["rows"][0]["run_id"] = retry_2_spend_id
    extra_retry.append(extra_retry_record)
    with pytest.raises(ValueError, match="only RISE-02 retry"):
        render_report(extra_retry, not_run, metadata, morning)


def test_report_allows_no_prior_retry_but_rejects_an_unapproved_second_question_attempt():
    morning = _morning_records()
    record = _retry_attempt("NOW-01", "NOW-01", "prefix-now", "run-now", "PASS", "complete", 0.1)
    metadata = _metadata(charged_usd=0.1, not_run_reasons={
        question_id: "operator_gate_closed" for question_id in QUESTION_IDS if question_id != "NOW-01"
    })
    report = render_report([record], ["RISE-02", "WHY-03", "SPR-03", "CRE-01"], metadata, morning)
    assert "| NOW-01 | R2 verdict: full answer with local receipts" in report

    retry = copy.deepcopy(record)
    retry.update({"attempt_key": "NOW-01-retry-1", "prefix": "prefix-now-retry", "run_id": "run-now-retry"})
    retry["run"]["run_id"] = "run-now-retry"
    retry_spend_id = "prefix-now-retry-spend-NOW-01-retry-1"
    retry["spend_writes"][0]["run_id"] = retry_spend_id
    retry["spend_readback"]["rows"][0]["run_id"] = retry_spend_id
    with pytest.raises(ValueError, match="only RISE-02 retry"):
        render_report([record, retry], ["RISE-02", "WHY-03", "SPR-03", "CRE-01"], metadata, morning)
