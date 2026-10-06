"""The one 42 answer contract: schema check plus cross-field integrity (rubric.md sections 1, 3 and 5)."""

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "eval" / "answer.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)
# Same pattern as answer_contract.mjs, so Python and the app accept exactly the same date-times.
ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:[.,]\d+)?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$")


def normalise(text):
    """Same normaliser as the citation_integrity assert in promptfooconfig.yaml."""
    text = unicodedata.normalize("NFC", str(text or ""))
    text = re.sub("[‘’‚‛‹›]", "'", text)
    text = re.sub("[“”„‟«»]", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def _datetime_problem(where, value):
    # jsonschema only checks date-time when rfc3339-validator is installed, and it is not.
    if not isinstance(value, str):
        return None
    match = ISO.match(value)
    try:
        datetime.fromisoformat(match.group(1)) if match else None
    except ValueError:
        match = None
    if not match:
        return f"{where}: {value!r} is not an ISO 8601 date-time"
    if not match.group(2) or not match.group(3):
        return f"{where}: {value!r} needs a time and a UTC offset"
    return None


def _dicts(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _ids(value):
    # Non-string ids are already schema problems; skipping them keeps set and dict lookups safe.
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def validate_answer(answer: dict) -> list[str]:
    """Return every problem with the answer; an empty list means it is valid."""
    problems = []
    for error in sorted(VALIDATOR.iter_errors(answer), key=lambda e: list(map(str, e.path))):
        where = "/".join(map(str, error.path)) or "answer"
        problems.append(f"{where}: {error.message}")
    if not isinstance(answer, dict):
        return problems

    claims = _dicts(answer.get("claims"))
    evidence = _dicts(answer.get("evidence"))

    problems.append(_datetime_problem("as_of", answer.get("as_of")))
    for i, record in enumerate(evidence):
        problems.append(_datetime_problem(f"evidence/{i}/posted_at", record.get("posted_at")))

    for kind, items in (("claim", claims), ("evidence", evidence)):
        seen = set()
        for item_id in _ids([item.get("id") for item in items]):
            if item_id in seen:
                problems.append(f"duplicate {kind} id {item_id!r}")
            seen.add(item_id)

    records = {r["id"]: r for r in evidence if isinstance(r.get("id"), str)}
    for claim in claims:
        cid = claim.get("id")
        cited = _ids(claim.get("evidence_ids"))
        for eid in cited:
            if eid not in records:
                problems.append(f"{cid}: evidence {eid!r} does not resolve to a post record")
        for quote in _dicts(claim.get("quotes")):
            eid = quote.get("evidence_id")
            if not isinstance(eid, str):
                continue
            if eid not in cited:
                problems.append(f"{cid}: quote attributed to {eid!r}, which the claim does not cite")
            elif eid in records and normalise(quote.get("text")) not in normalise(records[eid].get("text")):
                problems.append(f"{cid}: quote not found verbatim in record {eid!r}")

    claim_ids = set(_ids([c.get("id") for c in claims]))
    for section in ("so_what", "watch_next"):
        for i, item in enumerate(_dicts(answer.get(section))):
            for ref in _ids(item.get("claim_ids")):
                if ref not in claim_ids:
                    problems.append(f"{section}/{i}: claim id {ref!r} does not resolve")

    if answer.get("status") == "complete" and not claims:
        problems.append("status is complete but the answer has no claims")

    return [p for p in problems if p]
