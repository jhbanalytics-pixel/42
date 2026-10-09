"""Reference builder for N24's per-post own-word reaction records. Normative for lane 4; core/brief/reaction.py."""

import hashlib
import unicodedata

from core.trust.claims import _norm, _quote_fault  # the checks K1 and specificity already use

RECORD_VERSION = 1
NEWS_MIN_CREATORS = 2


def creator_key(platform, handle):
    h = (handle or "").strip().lstrip("@")
    if h.startswith("u/"):
        h = h[2:]
    p = "x" if platform == "twitter" else platform
    return f"{p}:{h.lower()}" if h and p else None


def span_hash(text):
    return hashlib.sha256(unicodedata.normalize("NFKC", _norm(text)).encode("utf-8")).hexdigest()


def reaction_records(*, claims, rests_on, supported_claim_ids, evidence, market, window, event_kind, local_ids):
    """One record per (post, span) that a resting, supported claim cites with a verbatim quote, by a located or
    feed-local creator who is not an outlet, not paid, not flagged and not a near duplicate, whose span does not
    appear in any outlet post of the pack. Every input but event_kind (the critic's boolean) is code-derived."""
    by_id = {r["id"]: r for r in evidence}
    outlet_text = [_norm(r.get("quote_text") or r.get("text")) for r in evidence if r.get("outlet_class") == "outlet"]
    out, seen = [], set()
    for claim in claims:
        if claim["id"] not in rests_on or claim["id"] not in supported_claim_ids:
            continue
        cited = set(claim.get("evidence_ids") or [])
        for q in claim.get("quotes") or []:
            pid, span = q.get("evidence_id"), _norm(q.get("text"))
            rec = by_id.get(pid)
            if rec is None or pid not in cited or pid not in local_ids or (pid, span) in seen:
                continue
            if rec.get("outlet_class") != "creator" or {"sponsored", "flagged", "near_duplicate"} & set(rec.get("flags") or []):
                continue
            key = creator_key(rec.get("platform"), rec.get("handle"))
            if key is None or _quote_fault(span, rec.get("text")) is not None:
                continue
            if any(span in text for text in outlet_text):
                continue
            seen.add((pid, span))
            out.append({"record_version": RECORD_VERSION, "post_id": pid, "creator_key": key, "market": market,
                        "window": list(window), "event_kind": event_kind, "span_hash": span_hash(span),
                        "claim_id": claim["id"], "support": "supported"})
    return out


def reacting_creators(records):
    return len({r["creator_key"] for r in records})
