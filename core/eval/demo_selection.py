import copy
import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


DEMO_QUESTION_IDS = frozenset({"DEMO-01", "DEMO-02", "DEMO-03", "DEMO-04", "DEMO-05"})
_ANSWER_STATUSES = frozenset({"complete", "partial", "insufficient_evidence", "refused"})


def _raw_hash(raw_answer):
    try:
        encoded = json.dumps(raw_answer, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalise_text(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def _claim_facts(answer, admitted_ids):
    claims = answer.get("claims")
    evidence = answer.get("evidence")
    if not isinstance(claims, list) or not isinstance(evidence, list):
        return {}, "The answer does not contain comparable claim and evidence records."

    evidence_by_id = {row.get("id"): row for row in evidence if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    claim_rows = {}
    for claim in claims:
        if isinstance(claim, Mapping) and isinstance(claim.get("id"), str):
            claim_rows.setdefault(claim["id"], []).append(claim)

    facts = {}
    for claim_id in admitted_ids:
        rows = claim_rows.get(claim_id, [])
        if len(rows) != 1:
            return {}, "An admitted claim is missing or ambiguous in its answer."
        claim = rows[0]
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return {}, "An admitted claim has no resolvable source evidence."
        evidence_id_set = {item for item in evidence_ids if isinstance(item, str)}
        number_facts = set()
        for number in claim.get("numbers", []):
            if not isinstance(number, Mapping):
                continue
            value = number.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
                continue
            try:
                normalized_value = str(Decimal(str(value)).normalize())
            except InvalidOperation:
                continue
            unit = number.get("unit")
            query_id = number.get("query_id")
            run_id = number.get("run_id")
            result_hash = number.get("result_hash")
            if not all(isinstance(item, str) and item for item in (unit, query_id, run_id, result_hash)):
                continue
            number_facts.add((query_id, normalized_value, unit, result_hash))

        quote_facts = set()
        for quote in claim.get("quotes", []):
            if not isinstance(quote, Mapping):
                continue
            evidence_id = quote.get("evidence_id")
            quote_text = _normalise_text(quote.get("text"))
            source = evidence_by_id.get(evidence_id)
            if not quote_text or evidence_id not in evidence_id_set or not isinstance(source, Mapping):
                continue
            source_text = _normalise_text(source.get("text"))
            if quote_text in source_text:
                quote_facts.add((evidence_id, quote_text))

        if not number_facts and not quote_facts:
            return {}, "An admitted claim has no verifiable number receipt or verbatim source quote."
        facts[claim_id] = {"numbers": frozenset(number_facts), "quotes": frozenset(quote_facts)}
    return facts, None


def _numeric_conflict(first, second):
    first_numbers = first["numbers"]
    second_numbers = second["numbers"]
    if not first_numbers or not second_numbers:
        return False
    first_by_query_unit = {(query_id, unit): value for query_id, value, unit, _ in first_numbers}
    second_by_query_unit = {(query_id, unit): value for query_id, value, unit, _ in second_numbers}
    shared = first_by_query_unit.keys() & second_by_query_unit.keys()
    return any(first_by_query_unit[key] != second_by_query_unit[key] for key in shared)


def _prepare_attempt(receipt, expected_attempt, question_id, assessor):
    result = {
        "attempt": expected_attempt,
        "run_id": None,
        "raw_hash": None,
        "call_costs": None,
        "valid": False,
        "reasons": [],
        "admitted_claim_ids": [],
        "rejections": [],
        "assessment_safe": False,
        "assessment_reasons": [],
        "candidate": None,
        "facts": None,
    }
    if receipt is None:
        result["reasons"].append("receipt_missing")
        return result
    if not isinstance(receipt, Mapping):
        result["reasons"].append("receipt_must_be_an_object")
        return result

    run_id = receipt.get("run_id")
    if isinstance(run_id, str) and run_id:
        result["run_id"] = run_id
    if "call_costs" in receipt:
        result["call_costs"] = copy.deepcopy(receipt["call_costs"])
    answer = receipt.get("raw_answer")
    if isinstance(answer, Mapping):
        result["raw_hash"] = _raw_hash(answer)

    if receipt.get("question_id") != question_id:
        result["reasons"].append("question_id_mismatch")
    if receipt.get("attempt") != expected_attempt:
        result["reasons"].append("attempt_number_mismatch")
    if result["run_id"] is None:
        result["reasons"].append("run_id_missing")
    if not isinstance(answer, Mapping):
        result["reasons"].append("raw_answer_missing")
    elif answer.get("status") not in _ANSWER_STATUSES:
        result["reasons"].append("answer_status_invalid")
    if result["raw_hash"] is None:
        result["reasons"].append("raw_answer_hash_unavailable")
    if result["reasons"]:
        return result

    assessment = assessor(receipt, question_id)
    if not isinstance(assessment, Mapping):
        result["reasons"].append("assessor_result_invalid")
        return result

    claims = answer.get("claims")
    claims = claims if isinstance(claims, list) else []
    claim_ids = [claim.get("id") for claim in claims if isinstance(claim, Mapping) and isinstance(claim.get("id"), str)]
    known_ids = set(claim_ids)
    raw_admitted = assessment.get("admitted_claim_ids")
    raw_admitted = raw_admitted if isinstance(raw_admitted, list) else []
    requested_ids = {item for item in raw_admitted if isinstance(item, str)}
    result["admitted_claim_ids"] = list(dict.fromkeys(item for item in claim_ids if item in requested_ids))

    raw_dropped = assessment.get("dropped")
    explicit_dropped = []
    if isinstance(raw_dropped, list):
        for row in raw_dropped:
            if isinstance(row, Mapping) and isinstance(row.get("claim_id"), str):
                explicit_dropped.append({"claim_id": row["claim_id"], "reason": str(row.get("reason", "not_admitted"))})
    dropped_ids = {row["claim_id"] for row in explicit_dropped}
    result["rejections"] = explicit_dropped + [
        {"claim_id": claim_id, "reason": "not_admitted"}
        for claim_id in dict.fromkeys(claim_ids)
        if claim_id not in requested_ids and claim_id not in dropped_ids
    ]
    result["rejections"].extend(
        {"claim_id": item, "reason": "admitted_claim_missing_from_answer"}
        for item in sorted(requested_ids - known_ids)
    )
    result["assessment_safe"] = assessment.get("safe") is True
    reasons = assessment.get("reasons")
    if isinstance(reasons, list):
        result["assessment_reasons"] = [str(reason) for reason in reasons]
    result["valid"] = True

    if answer.get("status") == "refused":
        result["reasons"].append("answer_refused")
        return result
    admitted_ids = result["admitted_claim_ids"]
    if not admitted_ids:
        result["reasons"].append("no_admitted_claims")
        return result

    claims_by_id = {}
    for claim in claims:
        if isinstance(claim, Mapping) and isinstance(claim.get("id"), str):
            claims_by_id.setdefault(claim["id"], []).append(claim)
    ambiguous_ids = [claim_id for claim_id in admitted_ids if len(claims_by_id.get(claim_id, [])) != 1]
    if ambiguous_ids:
        result["reasons"].append("admitted_claim_id_ambiguous")
        result["valid"] = False
        result["rejections"].extend(
            {"claim_id": claim_id, "reason": "admitted_claim_id_ambiguous"}
            for claim_id in ambiguous_ids
        )
        return result

    selected_claims = [copy.deepcopy(claims_by_id[claim_id][0]) for claim_id in admitted_ids]
    if not selected_claims:
        result["reasons"].append("admitted_claims_not_materialized")
        result["valid"] = False
        return result

    facts, fact_error = _claim_facts(answer, admitted_ids)
    result["facts"] = facts
    if fact_error:
        result["reasons"].append("source_grounding_unproven")
        result["fact_reason"] = fact_error

    selected_evidence_ids = {
        evidence_id
        for claim in selected_claims
        for evidence_id in claim.get("evidence_ids", [])
        if isinstance(evidence_id, str)
    }
    evidence = answer.get("evidence")
    selected_evidence = [
        copy.deepcopy(row)
        for row in evidence or []
        if isinstance(row, Mapping) and row.get("id") in selected_evidence_ids
    ] if isinstance(evidence, list) else []
    payload_status = answer["status"]
    if payload_status == "complete" and (len(selected_claims) < len(claims) or result["rejections"]):
        payload_status = "partial"
    result["candidate"] = {
        "attempt": expected_attempt,
        "score": len(selected_claims),
        "payload": {
            "question_id": question_id,
            "status": payload_status,
            "claims": selected_claims,
            "evidence": selected_evidence,
        },
    }
    return result


def select_pair(question_id, first, second, assessor=None):
    slots = {"1": first, "2": second}
    if assessor is None and question_id in DEMO_QUESTION_IDS:
        from core.eval.demo_scope import assess_answer

        assessor = assess_answer

    attempts = {}
    for attempt_key, receipt in slots.items():
        attempt_number = int(attempt_key)
        if question_id not in DEMO_QUESTION_IDS:
            attempt = _prepare_attempt(receipt, attempt_number, question_id, lambda *_: {})
            attempt["valid"] = False
            attempt["reasons"].append("question_outside_demo_scope")
        else:
            attempt = _prepare_attempt(receipt, attempt_number, question_id, assessor)
        attempts[attempt_key] = attempt

    first_id = attempts["1"]["run_id"]
    second_id = attempts["2"]["run_id"]
    if first_id is not None and first_id == second_id:
        attempts["1"]["valid"] = False
        attempts["2"]["valid"] = False
        attempts["1"]["reasons"].append("duplicate_run_id")
        attempts["2"]["reasons"].append("duplicate_run_id")
        attempts["1"]["candidate"] = None
        attempts["2"]["candidate"] = None

    candidates = [attempt["candidate"] for attempt in attempts.values() if attempt["valid"] and attempt["candidate"]]
    selected = max(candidates, key=lambda row: (row["score"], row["attempt"] == 1), default=None)
    selected_attempt = selected["attempt"] if selected else None

    stability_status = "unproven"
    if not all(attempts[key]["valid"] for key in ("1", "2")):
        stability_reason = "Both valid attempt receipts are required to assess stability."
    elif any(attempts[key]["reasons"] for key in ("1", "2")):
        stability_reason = "A refusal or answer without admitted claims cannot establish stability."
    elif not all(attempts[key]["assessment_safe"] for key in ("1", "2")):
        stability_reason = "At least one attempt was not fully assessed as safe."
    elif attempts["1"]["admitted_claim_ids"] != attempts["2"]["admitted_claim_ids"]:
        stability_reason = "The attempts admit different claim sets."
    elif attempts["1"]["facts"] is None or attempts["2"]["facts"] is None:
        stability_reason = "At least one attempt lacks comparable source-grounded facts."
    else:
        first_facts = attempts["1"]["facts"]
        second_facts = attempts["2"]["facts"]
        conflict = False
        comparable = True
        for claim_id in attempts["1"]["admitted_claim_ids"]:
            one = first_facts.get(claim_id)
            two = second_facts.get(claim_id)
            if one is None or two is None:
                comparable = False
                break
            if _numeric_conflict(one, two):
                conflict = True
                break
            if one != two:
                comparable = False
                break
        if conflict:
            stability_status = "unstable"
            stability_reason = "An admitted numeric fact differs between attempts."
        elif comparable:
            stability_status = "stable"
            stability_reason = "Both safe attempts admit the same claims with matching source-grounded facts."
        else:
            stability_reason = "Source-grounded facts differ or cannot be compared without interpreting wording."

    selected_payload = copy.deepcopy(selected["payload"]) if selected else None
    selected_candidate_reason = None
    if selected:
        selected_candidate_reason = "Most admitted claims; attempt 1 wins ties."
    elif any(attempts[key]["reasons"] and "answer_refused" in attempts[key]["reasons"] for key in ("1", "2")):
        selected_candidate_reason = "No non-refusal answer contains admitted claims."
    else:
        selected_candidate_reason = "No answer contains admitted claims."

    run_ids = {key: attempts[key]["run_id"] for key in ("1", "2")}
    raw_hashes = {key: attempts[key]["raw_hash"] for key in ("1", "2")}
    call_costs = {key: attempts[key]["call_costs"] for key in ("1", "2")}
    admitted_claim_ids = {key: attempts[key]["admitted_claim_ids"] for key in ("1", "2")}
    rejections = {key: attempts[key]["rejections"] for key in ("1", "2")}

    return {
        "question_id": question_id,
        "status": "selected" if selected else "unproven",
        "selected_attempt": selected_attempt,
        "selection_reason": selected_candidate_reason,
        "stability_status": stability_status,
        "stability_reason": stability_reason,
        "run_ids": run_ids,
        "raw_hashes": raw_hashes,
        "call_costs": call_costs,
        "admitted_claim_ids": admitted_claim_ids,
        "rejections": rejections,
        "receipt_validity": {key: attempts[key]["valid"] for key in ("1", "2")},
        "receipt_reasons": {key: attempts[key]["reasons"] for key in ("1", "2")},
        "assessment_reasons": {key: attempts[key]["assessment_reasons"] for key in ("1", "2")},
        "saved_payload": selected_payload,
        "full_pass": False,
        "l5_handoff": {
            "target": "L5",
            "question_id": question_id,
            "selected_attempt": selected_attempt,
            "stability_status": stability_status,
            "requires_review": True,
            "persistence_owner": "W6",
            "persisted": False,
            "full_pass": False,
        },
    }
