import importlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone

from core.agent.answer import validate_answer
from core.api import dossiers

_PRODUCER = "core.agent.saved_findings"
_ROW_FIELDS = {"finding_id", "question", "answer", "as_of", "claims", "valid_from", "valid_to", "status"}
_CLAIM_FIELDS = {"text", "label", "item_ids", "evidence_post_ids", "query_ids", "run_ids", "result_hashes"}
_ARRAY_FIELDS = ("item_ids", "evidence_post_ids", "query_ids", "run_ids", "result_hashes")


def _refused(message, status=409, code="not_eligible"):
    raise dossiers.Refused(status, code, message)


def _timestamp(value, *, allow_naive=False):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError as exc:
            raise ValueError("timestamp is malformed") from exc
    else:
        raise ValueError("timestamp is malformed")
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timestamp must include a timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="auto").replace("+00:00", "Z")


def _source(ask_id, record):
    if not isinstance(ask_id, str) or not ask_id.strip():
        _refused("A saved Ask ID is required.")
    if not isinstance(record, dict) or record.get("ask_id") != ask_id:
        _refused("The saved Ask does not match the requested ID.")
    if record.get("skin_id") is not None:
        _refused("Skin-backed Ask answers cannot become shared findings.")
    if record.get("investigation_id") is not None or record.get("parent_id") is not None:
        _refused("Only direct Ask answers can become findings.")
    if record.get("status") != "complete":
        _refused("Only a completed saved Ask can become a finding.")
    if not isinstance(record.get("question"), str) or not record["question"].strip():
        _refused("The saved Ask has no valid question.")
    if not isinstance(record.get("run"), dict):
        _refused("The saved Ask has no run record.")

    answer = record.get("answer")
    if not isinstance(answer, dict) or answer.get("status") not in ("complete", "partial"):
        _refused("Only a complete or partial answer can become a finding.")
    try:
        _timestamp(answer.get("as_of"))
    except ValueError:
        _refused("The saved answer has no valid as_of timestamp.")
    problems = validate_answer(answer)
    if problems:
        _refused("The saved answer is malformed: " + "; ".join(problems))
    if any(claim.get("check") == "cut" for claim in answer["claims"] if isinstance(claim, dict)):
        _refused("The saved answer contains a claim cut by its checks.")
    return answer


def _prepare(ask_id, record):
    try:
        module = importlib.import_module(_PRODUCER)
        prepare = module.prepare_saved_ask_finding
    except (ImportError, AttributeError):
        _refused("The checked-finding producer is not available yet.", 503, "producer_not_ready")
    try:
        return prepare(ask_id, record)
    except ValueError as exc:
        _refused(str(exc) or "The saved Ask is not eligible for a finding.")


def _producer_evidence_ids(row, answer, question, as_of, finished_at):
    if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
        _refused("The checked-finding producer returned a malformed row.")
    finding_id = row.get("finding_id")
    if not isinstance(finding_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", finding_id):
        _refused("The checked-finding producer returned an invalid finding ID.", 503, "producer_output_invalid")
    if row.get("question") != question or not isinstance(row.get("answer"), str) or not row["answer"].strip():
        _refused("The checked-finding producer returned malformed answer text.")
    if row.get("status") != "current" or row.get("valid_to") is not None:
        _refused("The checked-finding producer returned an invalid finding status.")
    try:
        if _timestamp(row.get("as_of"), allow_naive=True) != as_of:
            _refused("The checked-finding producer changed the source as_of timestamp.")
        if _timestamp(row.get("valid_from"), allow_naive=True) != _timestamp(finished_at):
            _refused("The checked-finding producer changed the source validity start.")
    except ValueError:
        _refused("The checked-finding producer returned a malformed timestamp.")

    claims = row.get("claims")
    if not isinstance(claims, list) or not claims:
        _refused("The checked-finding producer returned no claims.")
    source_evidence = {item["id"] for item in answer["evidence"]}
    cited = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != _CLAIM_FIELDS:
            _refused("The checked-finding producer returned a malformed claim.")
        if not isinstance(claim.get("text"), str) or not claim["text"].strip():
            _refused("The checked-finding producer returned an empty claim.")
        if claim.get("label") not in dossiers.LABELS:
            _refused("The checked-finding producer returned an unknown claim label.")
        for field in _ARRAY_FIELDS:
            value = claim.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                _refused("The checked-finding producer returned malformed claim provenance.")
        if claim["item_ids"]:
            _refused("The checked-finding producer returned item IDs that the Ask does not hold.")
        ids = claim["evidence_post_ids"]
        if not ids or len(set(ids)) != len(ids):
            _refused("The checked-finding producer returned missing or duplicate evidence IDs.")
        for evidence_id in ids:
            if evidence_id not in source_evidence:
                _refused("The checked-finding producer cited evidence absent from the saved Ask.")
            if evidence_id not in cited:
                cited.append(evidence_id)
    return cited


def evidence_ids(ask_id, record):
    answer = _source(ask_id, record)
    as_of = _timestamp(answer["as_of"])
    row = _prepare(ask_id, record)
    return _producer_evidence_ids(row, answer, record["question"], as_of, record.get("finished_at"))


def build(ask_id, record, posts):
    answer = _source(ask_id, record)
    as_of = _timestamp(answer["as_of"])
    row = _prepare(ask_id, record)
    cited = _producer_evidence_ids(row, answer, record["question"], as_of, record.get("finished_at"))
    if not isinstance(posts, list):
        _refused("The History evidence readback is malformed.")
    available = {post.get("post_id") for post in posts if isinstance(post, dict) and isinstance(post.get("post_id"), str)}
    missing = [evidence_id for evidence_id in cited if evidence_id not in available]
    if missing:
        _refused("Some checked evidence is not available to History: " + ", ".join(missing))
    return row


def _as_mapping(value):
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(value.items())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("finding row is malformed") from exc


def _normalised(row):
    row = _as_mapping(row)
    if set(row) != _ROW_FIELDS:
        raise ValueError("finding row fields do not match the schema")
    out = dict(row)
    for field in ("as_of", "valid_from", "valid_to"):
        if out[field] is not None:
            out[field] = _timestamp(out[field], allow_naive=True)
    if not isinstance(out["claims"], (list, tuple)):
        raise ValueError("finding claims are malformed")
    claims = []
    for claim in out["claims"]:
        claim = _as_mapping(claim)
        if set(claim) != _CLAIM_FIELDS:
            raise ValueError("finding claim fields do not match the schema")
        normalized_claim = dict(claim)
        for field in _ARRAY_FIELDS:
            value = normalized_claim[field]
            if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
                raise ValueError("finding claim provenance is malformed")
            normalized_claim[field] = list(value)
        claims.append(normalized_claim)
    out["claims"] = claims
    return json.dumps(out, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def same_row(expected, actual):
    try:
        return _normalised(expected) == _normalised(actual)
    except (OverflowError, TypeError, ValueError):
        return False
