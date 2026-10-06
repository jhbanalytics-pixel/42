"""Source ablation over recorded results (E01).

A pure kernel: remove one source family from a recorded result's receipts,
recompute which material claims still have support, and report the family's
unique supported contribution. The negative control projects only packet and
cited-family absence, then checks that the canonical packet is unchanged. It
does not prove upstream retrieval or admission absence. The paid E01 ablation
gate establishes those conditions before this kernel runs. Nothing here
regenerates an answer or touches a network; the paired regeneration is a
later, paid gate.
"""

import hashlib
import json

VERDICTS = frozenset({"supported", "contradicted", "unverified"})
RESULT_FIELDS = ("case_id", "packet_sources", "material_claims")


def canonical_packet_sha256(result):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_family(source_family):
    if not isinstance(source_family, str) or not source_family.strip():
        raise ValueError("source_family_invalid")


def _validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("result_record_invalid")
    for field in RESULT_FIELDS:
        if field not in result:
            raise ValueError(f"result_field_missing: {field}")
    packet = result["packet_sources"]
    if not isinstance(packet, list) or not all(
        isinstance(family, str) and family for family in packet
    ):
        raise ValueError("packet_sources_invalid")
    claims = result["material_claims"]
    if not isinstance(claims, list):
        raise ValueError("material_claims_list_required")
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            raise ValueError("claim_field_invalid: claim_id")
        if claim["claim_id"] in seen:
            raise ValueError("duplicate_claim_id")
        seen.add(claim["claim_id"])
        if claim.get("verdict") not in VERDICTS:
            raise ValueError("claim_verdict_invalid")
        receipts = claim.get("receipts")
        if not isinstance(receipts, list):
            raise ValueError("claim_receipts_required")
        for receipt in receipts:
            family = receipt.get("source_family") if isinstance(receipt, dict) else None
            if not isinstance(family, str) or not family:
                raise ValueError("receipt_source_family_required")
            if family not in packet:
                raise ValueError("receipt_outside_packet")


def ablate_source_family(result, source_family):
    """Recompute support with one family's receipts removed."""
    _validate_family(source_family)
    _validate_result(result)
    if source_family not in result["packet_sources"]:
        raise ValueError("ablation_source_absent")
    rows = []
    for claim in result["material_claims"]:
        remaining = [
            receipt
            for receipt in claim["receipts"]
            if receipt["source_family"] != source_family
        ]
        removed = len(claim["receipts"]) - len(remaining)
        after = claim["verdict"]
        if after == "supported" and not remaining:
            after = "unverified"
        rows.append(
            {
                "claim_id": claim["claim_id"],
                "verdict_before": claim["verdict"],
                "verdict_after": after,
                "receipts_removed": removed,
                "receipts_remaining": len(remaining),
            }
        )
    before = sum(row["verdict_before"] == "supported" for row in rows)
    after = sum(row["verdict_after"] == "supported" for row in rows)
    return {
        "case_id": result["case_id"],
        "source_family": source_family,
        "packet_changed": True,
        "packet_sources_after": [
            family for family in result["packet_sources"] if family != source_family
        ],
        "claims": rows,
        "supported_before": before,
        "supported_after": after,
        "unique_supported_contribution": before - after,
    }


def negative_control(result, source_family):
    """A family absent from the packet must leave everything unchanged."""
    _validate_family(source_family)
    _validate_result(result)
    cited = {
        receipt["source_family"]
        for claim in result["material_claims"]
        for receipt in claim["receipts"]
    }
    if source_family in result["packet_sources"] or source_family in cited:
        raise ValueError("negative_control_source_present")
    return {
        "case_id": result["case_id"],
        "source_family": source_family,
        "packet_changed": False,
        "claims_changed": 0,
        "canonical_packet_sha256": canonical_packet_sha256(result),
    }
