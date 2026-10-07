from core.trust.claims import _norm, _quote_fault, located_market, source_market


def local_posts(evidence, market):
    code = str(market or "").strip().upper()
    if not code or not isinstance(evidence, (list, tuple)):
        return []

    posts = []
    seen = set()
    for record in evidence or []:
        if not isinstance(record, dict):
            continue
        evidence_id = record.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in seen:
            continue
        seen.add(evidence_id)
        flags = record.get("flags")
        if flags is None:
            flags = []
        elif not isinstance(flags, (list, tuple)) or any(not isinstance(flag, str) for flag in flags):
            continue
        located_record = dict(record, flags=[flag.strip().lower() for flag in flags])
        located = located_market(located_record)
        if located == code or located is None and source_market(record) == code:
            posts.append(record)
    return posts


# BUILD.md 1.12: every trend shows at least 3 cited posts or is held back with its reason.
MIN_EVIDENCE = 3


def showable_posts(evidence, market):
    """The posts a card can show in the market: those with no known location and those local to it. A post located
    in another market is not shown as the market's."""
    local_ids = {post["id"] for post in local_posts(evidence, market)}
    return [record for record in evidence or [] if isinstance(record, dict)
            and (located_market(record) is None or record.get("id") in local_ids)]


def specificity_basis(*, explanation, claims, explanation_claim_ids, evidence, market):
    why_now = explanation.strip() if isinstance(explanation, str) and explanation.strip() else None
    result = {"local_evidence_ids": [], "quote": None, "why_now": why_now, "reason": None}
    if why_now is None:
        result["reason"] = "missing_explanation"
        return result

    claims_by_id = {}
    claim_records = claims if isinstance(claims, (list, tuple)) else []
    for claim in claim_records:
        if isinstance(claim, dict):
            claim_id = claim.get("id")
            if isinstance(claim_id, str) and claim_id.strip():
                claims_by_id.setdefault(claim_id, []).append(claim)

    if not isinstance(explanation_claim_ids, (list, tuple)) or any(
        not isinstance(claim_id, str) or not claim_id.strip() for claim_id in explanation_claim_ids
    ):
        result["reason"] = "unsupported_claim_reference"
        return result
    referenced_ids = list(dict.fromkeys(explanation_claim_ids))
    if not referenced_ids or any(len(claims_by_id.get(claim_id, [])) != 1 for claim_id in referenced_ids):
        result["reason"] = "unsupported_claim_reference"
        return result

    referenced_claims = [claims_by_id[claim_id][0] for claim_id in referenced_ids]
    local_by_id = {post["id"]: post for post in local_posts(evidence, market)}
    seen_local_ids = set()
    for claim in referenced_claims:
        claim_evidence_ids = claim.get("evidence_ids")
        if not isinstance(claim_evidence_ids, (list, tuple)):
            continue
        for evidence_id in claim_evidence_ids:
            if isinstance(evidence_id, str) and evidence_id in local_by_id and evidence_id not in seen_local_ids:
                seen_local_ids.add(evidence_id)
                result["local_evidence_ids"].append(evidence_id)
    if len(result["local_evidence_ids"]) < 2:
        result["reason"] = "insufficient_local_evidence"
        return result

    quote_failures = []
    for claim in referenced_claims:
        cited_ids = claim.get("evidence_ids")
        if isinstance(cited_ids, (list, tuple)):
            cited_ids = {evidence_id for evidence_id in cited_ids if isinstance(evidence_id, str)}
        else:
            cited_ids = set()
        quotes = claim.get("quotes")
        quote_records = quotes if isinstance(quotes, (list, tuple)) else []
        for quote in quote_records:
            if not isinstance(quote, dict):
                continue
            evidence_id = quote.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id not in cited_ids:
                quote_failures.append("quote_not_cited")
                continue
            if evidence_id not in local_by_id:
                quote_failures.append("quote_not_local")
                continue
            text = quote.get("text")
            record = local_by_id[evidence_id]
            full_source_text = record.get("quote_text")
            source_text = (
                full_source_text
                if isinstance(full_source_text, str) and full_source_text.strip()
                else record.get("text")
            )
            if not isinstance(text, str):
                quote_failures.append("quote_not_verbatim")
                continue
            words = text.split()
            if len(words) < 2:
                quote_failures.append("quote_too_short")
                continue
            if len(words) > 25 or len(text) > 160:
                quote_failures.append("quote_too_long")
                continue
            if not isinstance(source_text, str) or text not in source_text:
                quote_failures.append("quote_not_verbatim")
                continue
            fault = _quote_fault(_norm(text), source_text)
            if fault:
                quote_failures.append("quote_too_short" if fault == "under 2 words" else "quote_not_word_bounded")
                continue
            result["quote"] = {"evidence_id": evidence_id, "text": text}
            return result

    result["reason"] = quote_failures[0] if quote_failures else "missing_local_quote"
    return result


def assess_specificity(*, explanation, claims, explanation_claim_ids, evidence, market, local_why_now=False):
    result = specificity_basis(
        explanation=explanation,
        claims=claims,
        explanation_claim_ids=explanation_claim_ids,
        evidence=evidence,
        market=market,
    )
    if result["reason"] is None and local_why_now is not True:
        result["reason"] = "local_why_now_not_checked"
    result["status"] = "pass" if result["reason"] is None else "fail"
    return result
