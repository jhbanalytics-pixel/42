"""The server's reading of the in app reply validator.

validateIntelligenceReply in app/frontend/src/generalIntelligence.js decides
whether the Ask view shows a stored reply or withholds it. The server has no
JavaScript runtime, so this module states the same checks in the same order
with the same failure names, and follows JavaScript's own semantics where they
differ from Python's: string trimming, number types, UTC millisecond times and
the UTF-8 encoding of a quoted span. test_ask_export_admission runs the browser
function and this one over the same corpus and requires the same verdict and
the same failure name for every case, so the two cannot drift apart unseen.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date

VERSION = "general_cultural_question_v1"
SECTIONS = ("answer", "evidence", "interpretation", "actions")
STATUSES = ("complete", "partial", "needs_clarification", "unavailable", "refused")
RECEIPT_KEYS = (
    "receipt_id", "citation_label", "kind", "snapshot_id", "market", "source_label",
    "source_family", "platform", "author", "url", "source_row_id", "published_at",
    "collected_at", "excerpt", "reading_ids", "limitations", "content_digest",
)
BOUND_RECEIPT_KEYS = (*RECEIPT_KEYS, "evidence_purposes", "quote_bindings")
PURPOSES = ("support", "challenge", "context")
REPLY_KEYS = (
    "contract_version", "request_id", "request_digest", "status", "resolved_scope",
    "window", "as_of", "snapshot_id", "sections", "claims", "receipts", "readings",
    "limitations", "missing_work", "clarification", "review_required",
    "ready_for_downstream", "usage",
)
SCOPE_KEYS = ("client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids", "theme_id")
USAGE_KEYS = (
    "status", "model_calls", "input_tokens", "output_tokens", "usage_receipt_ids",
    "call_receipt_ids", "reservation_ids", "reserved_cost_usd", "reason",
)
BINDING_KEYS = ("claim_id", "content_digest", "end", "quote_sha256", "source_field", "start")
READING_KEYS = (
    "reading_id", "value", "unit", "window", "method", "denominator",
    "source_receipt_ids", "limitations",
)
CLAIM_KEYS = (
    "claim_id", "text", "kind", "receipt_ids", "reading_ids", "parent_claim_ids",
    "support_state", "limitations", "falsifier",
)
DAY_MS = 86400000
WITHHELD = "The structured reply is incomplete or inconsistent. Its answer has been withheld."

# String.prototype.trim removes WhiteSpace and LineTerminator code points only.
_JS_SPACE = frozenset(
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
_DIGEST = re.compile(r"[a-fA-F0-9]{64}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?"
    r"(?:Z|([+-])([0-9]{2}):([0-9]{2}))"
)
_MARKET = re.compile(r"[a-z]{2}")
_LABEL = re.compile(r"R[1-9][0-9]*")
_COST = re.compile(r"[0-9]+\.[0-9]{6}")
_EPOCH = date(1970, 1, 1).toordinal()
_NAME = re.compile(r"[a-z_]+")


class _Withheld(Exception):
    def __init__(self, field):
        super().__init__(field)
        self.field = field


def _require(condition, field):
    if not condition:
        raise _Withheld(field)


def _text(value):
    return isinstance(value, str) and any(char not in _JS_SPACE for char in value)


def _nullable_text(value):
    return value is None or _text(value)


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value)


def _unique(value):
    return _strings(value) and len(set(value)) == len(value)


def _number(value):
    """The JavaScript number a JSON value would parse to, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _finite(value):
    number = _number(value)
    return number is not None and math.isfinite(number)


def _integer(value):
    number = _number(value)
    return (
        number is not None
        and math.isfinite(number)
        and number.is_integer()
        and 0 <= number <= 2**53 - 1
    )


def _truthy(value):
    if value is None or value is False:
        return False
    number = _number(value)
    if number is not None:
        return number != 0 and not math.isnan(number)
    return not (isinstance(value, str) and value == "")


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _record(value):
    return isinstance(value, dict)


def _keys(value, names):
    return _record(value) and len(value) == len(names) and all(name in value for name in names)


def _day(value):
    """Milliseconds since the epoch at the start of a calendar day, or None."""
    if not isinstance(value, str) or _DAY.fullmatch(value) is None or int(value[:4]) < 1:
        return None
    try:
        return (date.fromisoformat(value).toordinal() - _EPOCH) * DAY_MS
    except ValueError:
        return None


def _timestamp(value):
    """Date.parse of an RFC 3339 time the validator admits, in whole milliseconds."""
    if not isinstance(value, str):
        return None
    shape = _STAMP.fullmatch(value)
    if shape is None:
        return None
    start = _day(shape.group(1))
    hour, minute, second = (int(shape.group(index)) for index in (2, 3, 4))
    offset_hour = int(shape.group(7) or 0)
    offset_minute = int(shape.group(8) or 0)
    if start is None or hour > 23 or minute > 59 or second > 59 or offset_hour > 23 or offset_minute > 59:
        return None
    millis = int((shape.group(5) or "0")[:3].ljust(3, "0"))
    offset = (offset_hour * 60 + offset_minute) * 60000
    if shape.group(6) == "-":
        offset = -offset
    return start + ((hour * 60 + minute) * 60 + second) * 1000 + millis - offset


def _window_valid(value):
    return (
        _keys(value, ("start", "end", "closed"))
        and _day(value["start"]) is not None
        and _day(value["end"]) is not None
        and _day(value["start"]) <= _day(value["end"])
        and isinstance(value["closed"], bool)
    )


def _index_rows(rows, field):
    _require(isinstance(rows, list), field)
    index = {}
    for row in rows:
        _require(_record(row) and _text(row.get(field)) and row[field] not in index, field)
        index[row[field]] = row
    return index


def quoted_span(excerpt, binding):
    return "".join(list(excerpt)[binding["start"] : binding["end"]])


def _utf8(value):
    # TextEncoder writes U+FFFD for a lone surrogate.
    return "".join("\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value).encode("utf-8")


def _cites(claims, claim_id, receipt):
    claim = claims.get(claim_id) if isinstance(claim_id, str) else None
    return (
        claim is not None
        and isinstance(claim.get("receipt_ids"), list)
        and receipt["receipt_id"] in claim["receipt_ids"]
    )


def _quote_bound(receipt, item, claims):
    return (
        _cites(claims, item["claim_id"], receipt)
        and _integer(item["start"])
        and _integer(item["end"])
        and item["end"] > item["start"]
        and item["end"] <= len(receipt["excerpt"])
        and item["quote_sha256"].lower()
        == hashlib.sha256(_utf8(quoted_span(receipt["excerpt"], _span_ints(item)))).hexdigest()
    )


def _span_ints(item):
    return {"start": int(item["start"]), "end": int(item["end"])}


def validate_intelligence_reply(value, *, terminal_error=False):
    """{"ok": True, ...indexes} when the Ask view would show the reply, else the failure name."""
    try:
        return _validate(value, terminal_error)
    except _Withheld as withheld:
        field = withheld.field if _NAME.fullmatch(withheld.field) else "unknown"
        return {"ok": False, "reason": WITHHELD, "field": field}
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        return {"ok": False, "reason": WITHHELD, "field": "unknown"}


def _validate(value, terminal_error):
    _require(_keys(value, REPLY_KEYS), "response")
    _require(
        value["contract_version"] == VERSION
        and isinstance(value["request_id"], str)
        and _UUID.fullmatch(value["request_id"]) is not None
        and _digest(value["request_digest"]),
        "request",
    )
    as_of = _timestamp(value["as_of"])
    _require(
        value["status"] in STATUSES
        and as_of is not None
        and (value["as_of"].endswith("Z") or value["as_of"].endswith("+00:00")),
        "status",
    )
    scope = value["resolved_scope"]
    _require(
        _keys(scope, SCOPE_KEYS)
        and _text(scope["client_scope_id"])
        and _unique(scope["market_scope"])
        and len(scope["market_scope"]) > 0
        and all(_MARKET.fullmatch(market) for market in scope["market_scope"])
        and _unique(scope["audience_lens_ids"])
        and _nullable_text(scope["brand_config_id"])
        and _nullable_text(scope["theme_id"]),
        "scope",
    )
    window = value["window"]
    _require(window is None or _window_valid(window), "window")
    _require(
        _nullable_text(value["snapshot_id"])
        and _strings(value["limitations"])
        and _strings(value["missing_work"])
        and _nullable_text(value["clarification"]),
        "context",
    )
    _require(
        isinstance(value["review_required"], bool)
        and isinstance(value["ready_for_downstream"], bool)
        and not (value["review_required"] and value["ready_for_downstream"]),
        "review",
    )
    answered = value["status"] in ("complete", "partial")
    _require(not (terminal_error and value["status"] == "complete"), "terminal_inconsistent")
    _require(not answered or (window is not None and _text(value["snapshot_id"])), "answered_scope")
    _require(
        window is None or not window["closed"] or _day(window["end"]) + DAY_MS <= as_of,
        "window_closure",
    )
    _require(value["status"] != "needs_clarification" or _text(value["clarification"]), "clarification")
    _require(answered or not value["ready_for_downstream"], "downstream")
    usage = value["usage"]
    _require(_keys(usage, USAGE_KEYS) and usage["status"] in ("resolved", "unresolved"), "usage")
    _require(
        _unique(usage["usage_receipt_ids"])
        and _unique(usage["call_receipt_ids"])
        and _unique(usage["reservation_ids"])
        and isinstance(usage["reserved_cost_usd"], str)
        and _COST.fullmatch(usage["reserved_cost_usd"]) is not None,
        "usage_receipts",
    )
    _require(
        all(
            _integer(usage[key]) or (usage["status"] == "unresolved" and usage[key] is None)
            for key in ("model_calls", "input_tokens", "output_tokens")
        ),
        "usage_counts",
    )
    _require(
        usage["reason"] is None
        if usage["status"] == "resolved"
        else usage["reason"] in ("response_usage_unavailable", "metering_persistence_failed"),
        "usage_reason",
    )
    unresolved = usage["status"] == "unresolved"
    _require(
        not unresolved or (value["status"] != "complete" and not value["ready_for_downstream"]),
        "unresolved_usage",
    )
    _require(
        not unresolved or (len(usage["call_receipt_ids"]) > 0 and len(usage["reservation_ids"]) > 0),
        "unresolved_attempt",
    )
    _require(
        usage["status"] != "resolved"
        or not _truthy(usage["model_calls"])
        or (
            len(usage["usage_receipt_ids"]) > 0
            and len(usage["call_receipt_ids"]) >= usage["model_calls"]
            and len(usage["reservation_ids"]) > 0
        ),
        "measured_usage_receipts",
    )
    claims = _index_rows(value["claims"], "claim_id")
    receipts = _index_rows(value["receipts"], "receipt_id")
    readings = _index_rows(value["readings"], "reading_id")

    def refs(ids, index):
        return _unique(ids) and all(item in index for item in ids)

    labels = set()
    for receipt in receipts.values():
        _require(_keys(receipt, RECEIPT_KEYS) or _keys(receipt, BOUND_RECEIPT_KEYS), "receipt")
        label = receipt["citation_label"]
        _require(
            isinstance(label, str) and _LABEL.fullmatch(label) is not None and label not in labels,
            "citation_label",
        )
        labels.add(label)
        _require(
            receipt["kind"] in ("content", "aggregate")
            and receipt["snapshot_id"] == value["snapshot_id"]
            and _text(receipt["snapshot_id"])
            and _digest(receipt["content_digest"])
            and _text(receipt["source_label"]),
            "receipt_identity",
        )
        _require(
            receipt["market"] is None
            or (isinstance(receipt["market"], str) and receipt["market"] in scope["market_scope"]),
            "receipt_market",
        )
        _require(
            all(
                _nullable_text(receipt[key])
                for key in ("source_family", "platform", "author", "url", "source_row_id", "excerpt")
            )
            and _strings(receipt["limitations"])
            and refs(receipt["reading_ids"], readings),
            "receipt_fields",
        )
        _require(receipt["kind"] != "content" or _text(receipt["source_row_id"]), "source_row")
        _require(
            all(
                receipt[key] is None
                or (_timestamp(receipt[key]) is not None and _timestamp(receipt[key]) <= as_of)
                for key in ("published_at", "collected_at")
            ),
            "receipt_dates",
        )
        if "quote_bindings" in receipt:
            purposes = receipt["evidence_purposes"]
            _require(
                isinstance(purposes, list)
                and all(
                    _keys(item, ("claim_id", "evidence_purpose"))
                    and _cites(claims, item["claim_id"], receipt)
                    and item["evidence_purpose"] in PURPOSES
                    for item in purposes
                ),
                "evidence_purposes",
            )
            bindings = receipt["quote_bindings"]
            _require(
                isinstance(bindings, list)
                and all(
                    _keys(item, BINDING_KEYS)
                    and isinstance(item["claim_id"], str)
                    and item["claim_id"] in claims
                    and item["content_digest"] == receipt["content_digest"]
                    and _digest(item["quote_sha256"])
                    and item["source_field"] == "excerpt"
                    and _text(receipt["excerpt"])
                    and _quote_bound(receipt, item, claims)
                    for item in bindings
                ),
                "quote_bindings",
            )
    for reading in readings.values():
        _require(_keys(reading, READING_KEYS), "reading")
        reading_value = reading["value"]
        _require(
            reading_value is None
            or isinstance(reading_value, (str, bool))
            or _finite(reading_value),
            "reading_value",
        )
        _require(
            _text(reading["unit"])
            and _text(reading["method"])
            and _window_valid(reading["window"])
            and _strings(reading["limitations"]),
            "reading_context",
        )
        _require(
            not reading["window"]["closed"] or _day(reading["window"]["end"]) + DAY_MS <= as_of,
            "reading_closure",
        )
        _require(
            reading["denominator"] is None
            or (_finite(reading["denominator"]) and _number(reading["denominator"]) >= 0),
            "denominator",
        )
        _require(
            refs(reading["source_receipt_ids"], receipts) and len(reading["source_receipt_ids"]) > 0,
            "reading_sources",
        )
    for claim in claims.values():
        _require(_keys(claim, CLAIM_KEYS), "claim")
        _require(
            _text(claim["text"])
            and claim["kind"] in ("observation", "interpretation", "inference", "proposal")
            and claim["support_state"] in ("source_record", "derived", "proposed"),
            "claim_kind",
        )
        _require(
            refs(claim["receipt_ids"], receipts)
            and refs(claim["reading_ids"], readings)
            and refs(claim["parent_claim_ids"], claims)
            and _strings(claim["limitations"])
            and _nullable_text(claim["falsifier"]),
            "claim_refs",
        )
        _require((claim["kind"] == "proposal") == (claim["support_state"] == "proposed"), "proposal_type")
        if claim["kind"] == "proposal":
            _require(value["review_required"] and not value["ready_for_downstream"], "proposal_review")
    grounded = _grounding(claims)
    sections = value["sections"]
    _require(isinstance(sections, list), "sections")
    selected, prior = set(), -1
    for section in sections:
        if section is None:
            raise TypeError("section")
        kind = section.get("kind") if _record(section) else None
        order = next((index for index, name in enumerate(SECTIONS) if name == kind), -1)
        _require(
            _keys(section, ("kind", "claim_ids"))
            and order > prior
            and refs(section["claim_ids"], claims)
            and len(section["claim_ids"]) > 0,
            "section_order",
        )
        prior = order
        for claim_id in section["claim_ids"]:
            _require(claim_id not in selected, "repeated_claim")
            selected.add(claim_id)
            if claims[claim_id]["kind"] == "proposal":
                _require(
                    section["kind"] == "actions"
                    and value["review_required"]
                    and not value["ready_for_downstream"],
                    "proposal_review",
                )
            else:
                _require(grounded[claim_id], "claim_basis")
    _require(
        not answered
        or any(
            section["kind"] == "answer" and any(grounded[claim_id] for claim_id in section["claim_ids"])
            for section in sections
        ),
        "grounded_answer",
    )
    return {"ok": True, "value": value, "claims": claims, "receipts": receipts, "readings": readings}


def _grounding(claims):
    """Whether each claim rests on a record, a reading or a grounded parent; a cycle withholds."""
    grounded, visiting = {}, set()
    for root in claims:
        if root in grounded:
            continue
        stack = [(root, 0)]
        visiting.add(root)
        while stack:
            claim_id, position = stack[-1]
            parents = claims[claim_id]["parent_claim_ids"]
            if position < len(parents):
                stack[-1] = (claim_id, position + 1)
                parent = parents[position]
                _require(parent not in visiting, "claim_cycle")
                if parent not in grounded:
                    visiting.add(parent)
                    stack.append((parent, 0))
                continue
            claim = claims[claim_id]
            grounded[claim_id] = claim["support_state"] != "proposed" and (
                len(claim["receipt_ids"]) > 0
                or len(claim["reading_ids"]) > 0
                or any(grounded[parent] for parent in parents)
            )
            visiting.discard(claim_id)
            stack.pop()
    return grounded
