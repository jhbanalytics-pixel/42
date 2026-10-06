"""Canonical bounded parent capsules. Context never grants source authority."""

import copy
import json
import re
from datetime import date

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    require_request_id,
)
from src.analysis.open_intelligence.general_question_window_comparison import (
    RETRIEVAL_COUNT_METHODS,
)

CAPSULE_VERSION = "general_question_parent_context_v1"
REF_VERSION = "general_question_parent_context_ref_v1"
_AUTHORITY = "Context only. Source records must pass current child admission; these previews do not approve claims."
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[1-9][0-9]*\Z")
_REFERENCE_DIGESTS = (
    "request_digest",
    "intake_digest",
    "plan_digest",
    "snapshot_digest",
    "result_digest",
)
_REFERENCE_GENERATIONS = (
    "request_generation",
    "intake_generation",
    "plan_generation",
    "snapshot_generation",
    "result_generation",
)
_OMISSIONS = {
    "server_receipt_refs_omitted",
    "claim_refs_omitted",
    "source_previews_omitted",
    "previews_truncated",
    "count_tainted_claims_withheld",
}


class ParentSourceUnavailable(ValueError):
    code = "parent_source_unavailable"

    def __init__(self):
        super().__init__(self.code)


def invalid():
    raise QuestionStoreError("parent_context_invalid")


def validate_parent_ref(value):
    if type(value) is not dict or set(value) != {
        "request_id",
        *_REFERENCE_DIGESTS,
        *_REFERENCE_GENERATIONS,
    }:
        invalid()
    require_request_id(value["request_id"])
    if any(
        type(value[key]) is not str or _DIGEST.fullmatch(value[key]) is None
        for key in _REFERENCE_DIGESTS
    ):
        invalid()
    if any(
        type(value[key]) is not str or _GENERATION.fullmatch(value[key]) is None
        for key in _REFERENCE_GENERATIONS
    ):
        invalid()
    return value


def validate_context_ref(value):
    if (
        type(value) is not dict
        or set(value)
        != {"contract_version", "parent", "anchor", "context_digest", "context_generation"}
        or value["contract_version"] != REF_VERSION
    ):
        invalid()
    validate_parent_ref(value["parent"])
    if value["anchor"] is not None:
        validate_parent_ref(value["anchor"])
    if (
        type(value["context_digest"]) is not str
        or _DIGEST.fullmatch(value["context_digest"]) is None
        or type(value["context_generation"]) is not str
        or _GENERATION.fullmatch(value["context_generation"]) is None
    ):
        invalid()
    return value


def planner_parent_view(capsule):
    frame = capsule["resolved_frame"]
    value = {
        "authority": _AUTHORITY,
        "resolved_frame": {
            key: frame[key] for key in ("origin_request_id", "markets", "window", "selected_market")
        },
        "source_previews": [
            {
                "alias": row["alias"],
                "origin_role": row["origin_role"],
                "platform": row["platform"],
                "market": row["market"],
                **row["source_preview"],
            }
            for row in capsule["parent_receipt_refs"]
            if row["source_preview"] is not None
        ],
        "preview_omissions": capsule["preview_omissions"],
    }
    if len(canonical_bytes(value)) > 4000:
        raise QuestionStoreError("parent_context_invalid")
    return copy.deepcopy(value)


def select_parent_aliases(capsule, aliases):
    visible = {
        row["alias"]: row
        for row in capsule["parent_receipt_refs"]
        if row["source_preview"] is not None
    }
    if (
        type(aliases) is not list
        or len(aliases) > 4
        or any(type(alias) is not str for alias in aliases)
        or len(set(aliases)) != len(aliases)
        or not set(aliases) <= visible.keys()
    ):
        raise QuestionStoreError("parent_alias_invalid")
    selected = [copy.deepcopy(visible[alias]) for alias in aliases]
    identities = {}
    for row in selected:
        key = row["market"], row["source_row_id"]
        value = row["lane"], row["content_digest"]
        if key in identities and identities[key] != value:
            raise QuestionStoreError("parent_alias_invalid")
        identities[key] = value
    return selected


def validate_capsule(value, *, request, reference=None):
    fields = {
        "contract_version",
        "child_request_id",
        "child_request_digest",
        "anchor",
        "parent",
        "resolved_frame",
        "parent_claim_refs",
        "parent_receipt_refs",
        "preview_omissions",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or value["contract_version"] != CAPSULE_VERSION
        or value["child_request_id"] != request["request_id"]
        or value["child_request_digest"] != request["request_digest"]
    ):
        invalid()
    validate_parent_ref(value["parent"])
    if value["anchor"] is not None:
        validate_parent_ref(value["anchor"])
    if any(
        ref is not None and ref["request_id"] == request["request_id"]
        for ref in (value["parent"], value["anchor"])
    ):
        invalid()
    if reference is not None:
        validate_context_ref(reference)
        if (
            reference["parent"] != value["parent"]
            or reference["anchor"] != value["anchor"]
            or canonical_digest(value) != reference["context_digest"]
        ):
            invalid()
    frame = value["resolved_frame"]
    if (
        type(frame) is not dict
        or set(frame) != {"origin_request_id", "markets", "window", "intent", "selected_market"}
        or frame["origin_request_id"] != value["parent"]["request_id"]
    ):
        invalid()
    markets = frame["markets"]
    if (
        type(markets) is not list
        or not markets
        or markets != sorted(set(markets))
        or not set(markets) <= set(request["market_scope"])
        or (
            frame["selected_market"] is not None
            and frame["selected_market"] not in request["market_scope"]
        )
    ):
        invalid()
    window = frame["window"]
    if (
        type(window) is not dict
        or set(window) != {"start", "end", "closed"}
        or type(window["closed"]) is not bool
        or type(frame["intent"]) is not str
    ):
        invalid()
    try:
        if date.fromisoformat(window["start"]) > date.fromisoformat(window["end"]):
            invalid()
    except (TypeError, ValueError):
        invalid()
    refs = value["parent_receipt_refs"]
    claims = value["parent_claim_refs"]
    if type(refs) is not list or len(refs) > 48 or type(claims) is not list or len(claims) > 8:
        invalid()
    aliases = set()
    visible = set()
    for row in refs:
        if (
            type(row) is not dict
            or set(row)
            != {
                "origin_role",
                "alias",
                "parent_receipt_id",
                "source_row_id",
                "lane",
                "market",
                "platform",
                "collected_at",
                "content_digest",
                "source_binding_digest",
                "source_preview",
            }
            or row["origin_role"] not in {"parent", "anchor"}
            or row["lane"] not in {"enriched_content", "raw_content"}
            or row["market"] not in request["market_scope"]
        ):
            invalid()
        if (
            type(row["alias"]) is not str
            or re.fullmatch(r"s[0-9]{2}", row["alias"]) is None
            or row["alias"] in aliases
        ):
            invalid()
        aliases.add(row["alias"])
        for key in ("content_digest", "source_binding_digest"):
            if type(row[key]) is not str or _DIGEST.fullmatch(row[key]) is None:
                invalid()
        for key in ("parent_receipt_id", "source_row_id", "collected_at"):
            if type(row[key]) is not str or not row[key]:
                invalid()
        if row["platform"] is not None and type(row["platform"]) is not str:
            invalid()
        preview = row["source_preview"]
        if preview is not None:
            if (
                type(preview) is not dict
                or set(preview)
                != {
                    "text",
                    "excerpt_start",
                    "excerpt_end",
                    "preview_truncated",
                    "preview_authority",
                }
                or type(preview["text"]) is not str
                or not preview["text"]
                or preview["preview_authority"] != "context_only"
                or type(preview["preview_truncated"]) is not bool
            ):
                invalid()
            if any(
                type(preview[key]) is not int or preview[key] < 0
                for key in ("excerpt_start", "excerpt_end")
            ) or preview["excerpt_end"] - preview["excerpt_start"] != len(preview["text"]):
                invalid()
            visible.add(row["alias"])
    if len(visible) > 8:
        invalid()
    for claim in claims:
        if (
            type(claim) is not dict
            or set(claim)
            != {
                "origin_role",
                "claim_id",
                "kind",
                "support_state",
                "parent_claim_ids",
                "receipt_aliases",
                "ancestry_class",
                "preview",
                "preview_truncated",
                "preview_authority",
            }
            or claim["origin_role"] not in {"parent", "anchor"}
            or claim["ancestry_class"] != "other"
            or claim["preview_authority"] != "context_only"
            or type(claim["receipt_aliases"]) is not list
            or not set(claim["receipt_aliases"]) <= visible
        ):
            invalid()
    omissions = value["preview_omissions"]
    if (
        type(omissions) is not dict
        or set(omissions) != _OMISSIONS
        or any(type(n) is not int or n < 0 for n in omissions.values())
    ):
        invalid()
    if omissions["source_previews_omitted"] != len(refs) - len(visible):
        invalid()
    planner_parent_view(value)
    return copy.deepcopy(value)


def build_capsule(request, parent, anchor=None):
    bundles = (
        [("parent", parent)]
        if anchor is None or anchor["reference"] == parent["reference"]
        else [("anchor", anchor), ("parent", parent)]
    )
    # A thread keeps the client lens it was asked under. A follow-up cannot take
    # context admitted under another lens, including general 42, in either direction.
    for _, bundle in bundles:
        if bundle.get("request", {}).get("client_lens") != request.get("client_lens"):
            invalid()
    parent_ids = {
        row["source_row_id"] for row in parent["result"]["response"]["intelligence"]["receipts"]
    }
    refs, claim_refs = [], []
    total_claims = tainted_count = input_refs = 0
    for role, bundle in bundles:
        intelligence = bundle["result"]["response"]["intelligence"]
        claims = {row["claim_id"]: row for row in intelligence["claims"]}
        readings = {row["reading_id"]: row for row in bundle["snapshot"]["readings"]}

        def tainted(cid, active=(), claims=claims, readings=readings):
            if cid in active:
                invalid()
            claim = claims[cid]
            return any(
                readings[key]["method"] in RETRIEVAL_COUNT_METHODS for key in claim["reading_ids"]
            ) or any(tainted(key, (*active, cid)) for key in claim["parent_claim_ids"])

        tainted_ids = {cid for cid in claims if tainted(cid)}
        total_claims += len(claims)
        tainted_count += len(tainted_ids)
        provenance = bundle["snapshot"]["provenance"]
        material = {
            (row["market"], row["id"]): (lane, json.loads(row["payload"]))
            for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content"))
            for row in provenance.get("captures", {}).get(key, {}).get("rows", [])
            if row.get("id") is not None
        }
        for receipt in intelligence["receipts"]:
            input_refs += 1
            if receipt["kind"] != "content" or provenance.get("profile") != "protected_context_v1":
                continue
            source = material.get((receipt["market"], receipt["source_row_id"]))
            if source is None:
                invalid()
            lane, payload = source
            quotes = sorted(
                (
                    claim
                    for cid, claim in claims.items()
                    if cid not in tainted_ids
                    and claim["kind"] == "observation"
                    and not claim["reading_ids"]
                    and claim["receipt_ids"] == [receipt["receipt_id"]]
                    and claim["text"] in (receipt.get("excerpt") or "")
                ),
                key=lambda claim: claim["claim_id"],
            )
            priority = (
                0
                if quotes and role == "anchor" and receipt["source_row_id"] not in parent_ids
                else 1
                if quotes and role == "parent"
                else 2
                if quotes
                else 3
            )
            refs.append(
                {
                    "origin_role": role,
                    "parent_receipt_id": receipt["receipt_id"],
                    "source_row_id": receipt["source_row_id"],
                    "lane": lane,
                    "market": receipt["market"],
                    "platform": payload["platform"],
                    "collected_at": payload["collected_at"],
                    "content_digest": receipt["content_digest"],
                    "source_binding_digest": provenance["source_binding"]["source_binding_digest"],
                    "source_preview": None,
                    "_priority": priority,
                    "_quotes": quotes,
                    "_excerpt": receipt.get("excerpt") or "",
                }
            )
    refs.sort(
        key=lambda row: (
            row["_priority"],
            row["source_row_id"],
            row["origin_role"],
            row["parent_receipt_id"],
        )
    )
    refs = refs[:48]
    seen = set()
    for index, row in enumerate(refs):
        row["alias"] = f"s{index + 1:02d}"
        identity = row["source_row_id"], row["content_digest"]
        if row["_quotes"] and len(claim_refs) < 8 and identity not in seen:
            claim = row["_quotes"][0]
            quote = claim["text"]
            preview = quote.encode("utf-8")[:180].decode("utf-8", errors="ignore")
            start = row["_excerpt"].find(quote)
            if start < 0:
                invalid()
            row["source_preview"] = {
                "text": preview,
                "excerpt_start": start,
                "excerpt_end": start + len(preview),
                "preview_truncated": preview != quote,
                "preview_authority": "context_only",
            }
            claim_refs.append(
                {
                    "origin_role": row["origin_role"],
                    "claim_id": claim["claim_id"],
                    "kind": claim["kind"],
                    "support_state": claim["support_state"],
                    "parent_claim_ids": list(claim["parent_claim_ids"]),
                    "receipt_aliases": [row["alias"]],
                    "ancestry_class": "other",
                    "preview": preview,
                    "preview_truncated": preview != quote,
                    "preview_authority": "context_only",
                }
            )
            seen.add(identity)
        for key in ("_priority", "_quotes", "_excerpt"):
            row.pop(key)
    value = {
        "contract_version": CAPSULE_VERSION,
        "child_request_id": request["request_id"],
        "child_request_digest": request["request_digest"],
        "parent": parent["reference"],
        "anchor": anchor["reference"] if anchor is not None else None,
        "resolved_frame": {
            "origin_request_id": parent["reference"]["request_id"],
            "markets": parent["plan"]["markets"],
            "window": parent["plan"]["window"],
            "intent": parent["plan"]["intent"],
            "selected_market": parent["intake"]["selected_market"],
        },
        "parent_claim_refs": claim_refs,
        "parent_receipt_refs": refs,
        "preview_omissions": {
            "server_receipt_refs_omitted": input_refs - len(refs),
            "claim_refs_omitted": total_claims - len(claim_refs),
            "source_previews_omitted": sum(row["source_preview"] is None for row in refs),
            "previews_truncated": sum(
                bool(row["source_preview"] and row["source_preview"]["preview_truncated"])
                for row in refs
            ),
            "count_tainted_claims_withheld": tainted_count,
        },
    }
    return validate_capsule(value, request=request)
