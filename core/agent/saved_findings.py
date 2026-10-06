from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import date, datetime

from core.agent.checks import REQUIRED_FIELDS
from core.api.dossiers import admitted

MARKETS = ("ZA", "NG", "KE")
RESULT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
WINDOW_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _timestamp(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a stored timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be a stored timestamp.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone.")
    return value


def _window(value):
    if not isinstance(value, dict):
        raise ValueError("The stored Ask run window is missing.")
    dates = []
    for key in ("from", "to"):
        raw = value.get(key)
        if not isinstance(raw, str) or not WINDOW_DATE.fullmatch(raw):
            raise ValueError("The stored Ask run window needs ISO dates.")
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            raise ValueError("The stored Ask run window needs ISO dates.") from None
    if dates[0] > dates[1]:
        raise ValueError("The stored Ask run window starts after it ends.")
    return copy.deepcopy(value)


def prepare_saved_ask_finding(ask_id, record) -> dict:
    if not isinstance(record, dict) or not isinstance(ask_id, str) or not ask_id:
        raise ValueError("A stored Ask record and ask id are required.")
    if record.get("skin_id") is not None:
        raise ValueError("Skin answers cannot be saved as Findings.")
    if record.get("ask_id") != ask_id:
        raise ValueError("The stored Ask id does not match.")
    if record.get("status") != "complete":
        raise ValueError("Only a completed Ask operation can be saved.")

    answer = record.get("answer")
    if not isinstance(answer, dict) or answer.get("status") not in {"complete", "partial"}:
        raise ValueError("Only a complete or partial checked answer can be saved.")
    question = record.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("The stored question is missing.")
    as_of = _timestamp(answer.get("as_of"), "answer.as_of")
    finished_at = _timestamp(record.get("finished_at"), "finished_at")
    run = record.get("run")
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("The stored Ask run id is missing.")
    window = _window(run.get("window") if isinstance(run, dict) else None)
    notices = run.get("notices", [])
    source_status = run.get("source_status", [])
    gaps = answer.get("gaps", [])
    if not isinstance(notices, list) or not isinstance(source_status, list) or not isinstance(gaps, list):
        raise ValueError("Stored Ask notices, source status and answer gaps must be lists.")

    claims_in = answer.get("claims")
    evidence_in = answer.get("evidence")
    if not isinstance(claims_in, list) or not isinstance(evidence_in, list):
        raise ValueError("The stored answer claims and evidence must be lists.")
    evidence_by_id = {}
    for evidence in evidence_in:
        evidence_id = evidence.get("id") if isinstance(evidence, dict) else None
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_by_id:
            raise ValueError("Stored answer evidence ids must resolve uniquely.")
        evidence_by_id[evidence_id] = evidence

    record_market = record.get("market")
    if record_market is not None and record_market not in MARKETS:
        raise ValueError("The stored Ask market is invalid.")

    admitted_claims, _ = admitted(answer)
    if len(admitted_claims) < 2:
        raise ValueError("At least two checked claims are required.")

    saved_claims = []
    located_post_ids = set()
    cited_evidence = {}
    for claim in admitted_claims.values():
        text, label = claim.get("text"), claim.get("label")
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(text, str) or not text.strip() or not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError("Every checked claim needs text and cited evidence.")

        cited_ids = []
        claim_located_ids = set()
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or evidence_id not in evidence_by_id:
                raise ValueError("Every cited evidence id must resolve to one stored post.")
            evidence = evidence_by_id[evidence_id]
            if any(not isinstance(evidence.get(field), str) or not evidence[field].strip()
                   for field in REQUIRED_FIELDS):
                raise ValueError("Every cited post must pass the K1 required fields.")
            flags = evidence.get("flags")
            if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
                raise ValueError("Every cited post needs a canonical flags list.")
            source_market = evidence.get("source_market")
            if source_market is not None and source_market not in MARKETS:
                raise ValueError("Cited post source markets must be valid or null.")
            market = evidence["market"]
            if market not in MARKETS or (record_market is not None and market != record_market):
                raise ValueError("Cited posts must have a valid market matching the stored Ask market.")
            if evidence.get("flags") == []:
                claim_located_ids.add(evidence_id)
                located_post_ids.add(evidence_id)
            if evidence_id not in cited_ids:
                cited_ids.append(evidence_id)
                cited_evidence[evidence_id] = copy.deepcopy(evidence)
        if not claim_located_ids:
            raise ValueError("Every checked claim needs an unflagged located post.")

        item_ids = claim.get("item_ids", [])
        if not isinstance(item_ids, list) or any(not isinstance(item_id, str) or not item_id.strip()
                                                 for item_id in item_ids):
            raise ValueError("Stored item ids must be nonempty strings.")

        numbers = claim.get("numbers", [])
        if not isinstance(numbers, list):
            raise ValueError("Stored claim numbers must be a list.")
        query_hashes = {}
        for number in numbers:
            if not isinstance(number, dict):
                raise ValueError("Stored number provenance must be an object.")
            query_id, number_run_id, result_hash = (number.get("query_id"), number.get("run_id"),
                                                     number.get("result_hash"))
            if (not isinstance(query_id, str) or not query_id.strip()
                    or number_run_id != run_id
                    or not isinstance(result_hash, str) or not RESULT_HASH.fullmatch(result_hash)):
                raise ValueError("Stored number query, run and result hash must be valid.")
            if query_id in query_hashes and query_hashes[query_id] != result_hash:
                raise ValueError("A stored query id cannot name multiple result hashes.")
            query_hashes[query_id] = result_hash

        saved_claims.append({
            "text": text,
            "label": label,
            "item_ids": list(dict.fromkeys(item_ids)),
            "evidence_post_ids": cited_ids,
            "query_ids": list(query_hashes),
            "run_ids": [run_id],
            "result_hashes": list(dict.fromkeys(query_hashes.values())),
        })

    if len(located_post_ids) < 2:
        raise ValueError("At least two distinct unflagged located posts are required.")

    saved_texts = [claim["text"] for claim in saved_claims]
    answer_text = "\n".join(saved_texts)
    if answer["status"] == "partial":
        answer_text = "Partial answer: " + answer_text
    envelope = {
        "schema_version": "saved-ask-finding-v1",
        "answer_text": answer_text,
        "source_ask_id": ask_id,
        "source_run_id": run_id,
        "parent_id": copy.deepcopy(record.get("parent_id")),
        "answer_status": answer["status"],
        "context": copy.deepcopy(answer.get("context")),
        "market": record_market,
        "window": window,
        "notices": copy.deepcopy(notices),
        "gaps": copy.deepcopy(gaps),
        "source_status": copy.deepcopy(source_status),
        "evidence": list(cited_evidence.values()),
    }
    try:
        encoded_answer = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("The checked answer context must contain finite JSON values.") from None
    row = {
        "question": question,
        "answer": encoded_answer,
        "as_of": as_of,
        "claims": saved_claims,
        "valid_from": finished_at,
        "valid_to": None,
        "status": "current",
    }
    canonical = json.dumps([ask_id, row], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    row["finding_id"] = "f_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {"finding_id": row["finding_id"], "question": row["question"], "answer": row["answer"],
            "as_of": row["as_of"], "claims": row["claims"], "valid_from": row["valid_from"],
            "valid_to": row["valid_to"], "status": row["status"]}
