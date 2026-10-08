"""What claim_checks keeps of a failed support or sentence check (W8-DEC-14).

The row keeps its verdict and claim id as before, plus span_sha256, the SHA-256 of the rejected span after NFKC and
whitespace normalisation, and reason_code, one member of REASON_CODES. It keeps no post text and no model words: the
span is hashed where it is rejected and never stored, and the code is worked out from the row's own rule, scope and
verdict, never taken from the model's reason.
"""

import hashlib
import re
import unicodedata

REASON_CODES = frozenset({
    "support_claim_unsupported", "support_claim_partial", "support_claim_other", "support_claim_crowd_wording",
    "support_sentence_unsupported", "support_sentence_partial", "support_sentence_other",
    "support_sentence_crowd_wording", "support_withheld_overflow",
    "sentence_number_unpinned", "sentence_place_outside_window", "sentence_place_other_market",
    "sentence_place_unlocated", "sentence_place_feed_wording", "sentence_place_source_only",
    "sentence_place_other", "sentence_banned_term", "sentence_translation", "sentence_future_assertion",
    "sentence_quote", "sentence_specificity",
    "critic_rival_not_ruled_out", "critic_why_now_not_shown", "critic_rival_and_why_now",
    "unclassified",
})

_DIGEST = re.compile(r"[0-9a-f]{64}")

# Rows that record a lowering or a title, not a rejection of the explanation or one of its claims.
NOT_RETAINED_RULES = frozenset({"title", "K5", "K10"})


def normalise_span(text):
    """The span as it is hashed: NFKC, then every run of whitespace as one space, trimmed. Case is kept."""
    if not isinstance(text, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).split())


def span_sha256(text):
    """The SHA-256 (lowercase hex) of the normalised span, or None when there is no span to hash."""
    span = normalise_span(text)
    return hashlib.sha256(span.encode("utf-8")).hexdigest() if span else None


def is_digest(value):
    """Shape only: 64 lowercase hex characters. It proves the value cannot be text, not that it hashes any span."""
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def is_retained(chk):
    """True for a failed support or sentence check: a cut or breach on the support check (K4) of a claim or of the
    sentence, or on any check of the explanation sentence itself (no claim id). A claim's code checks other than K4,
    the lowerings K5 and K10, and the title are not kept."""
    if chk.get("verdict") not in ("cut", "breach") or chk.get("rule") in NOT_RETAINED_RULES:
        return False
    return chk.get("rule") == "K4" or chk.get("claim_id") is None
